# Single Source of Truth Relationship Map

This document names the service layers that should own decisions. Web/API
routes and Celery tasks should be thin wrappers around these services.

HTTP is an adapter, not a service dependency. Domain and application services
must not import FastAPI/Starlette request, response, or exception types and must
not raise `HTTPException`. Owners return domain values or transport-neutral
errors with stable codes/context; HTTP routes translate those outcomes. Tasks,
webhooks, commands, and reconcilers call the same owners without inheriting HTTP
semantics.

The executable registry is `app/services/sot_relationships.py`. When a domain
is harmonised, add or update its service boundary there and cover it with tests
before migrating more callers.

## UI Projection Boundary

The approved cross-Dotmac presentation contract is
`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`.

1. Domain read/context services own displayed facts, status meaning,
   provenance, freshness, and business action hints.
2. Domain command/transition services own action eligibility and execution.
3. RBAC owns authorization. Every UI surface is granularly permission-gated:
   each list read, each bulk action, and each form/command action is gated by
   its own `domain:resource:action` permission projected against the principal.
   No coarse permission may span read and write (e.g. a single
   `operations:dispatch` covering list, create, update, and assign is a
   violation — split it into `:read`/`:write`/`:assign`). Event/timeline
   services own official history.
   During the reports permission migration, persisted `reports:billing` and
   `reports:network` API-key scopes are compatibility aliases for `:read` only;
   they never authorize `:export`. Database grants are migrated to the granular
   keys and the coarse permission rows are retired.
4. UI page contracts own relevance, ordering, progressive disclosure,
   responsive depth, and interaction shape.
5. Routes, templates, HTMX handlers, and mobile clients render the contract and
   submit commands; they do not derive business state, totals, or eligibility.
6. `ui.projection_contracts` owns the transport-neutral `StateValue`, `Kpi`, and
   `Action` shapes. Owners use them to distinguish unknown/stale/unavailable
   values, bind every KPI to its exact cohort, and separate action tone from
   eligibility and confirmation requirements.

Rule: the UI is a projection boundary, not a new business source of truth. Web,
API, exports, and mobile surfaces may present different depths for their task,
but equivalent state and actions resolve through the same backend owners.

## Domain Order

1. `party_identity`
2. `customer_context`
3. `financial_access`
4. `network`
5. `subscriber_sessions`
6. `application_sessions`
7. `secrets_credentials`
8. `notifications_communications`
9. `events_webhooks`
10. `runtime_infrastructure`
11. `observability`
12. `workforce_operations`
13. `support_operations`
14. `ai_advisory`
15. `provisioning_operations`
16. `feature_control_plane`
17. `authorization_control_plane`
18. `scheduler_control_plane`
19. `network_access_control_plane`
20. `service_intent_control_plane`
21. `integration_control_plane`
22. `ui_list_projection`
23. `ui_bulk_actions`
24. `ui_display_formatting`
25. `ui_action_forms`
26. `ui_semantic_presentation`
27. `vpn_remote_access`
28. `geospatial`
29. `sales_referrals`

Rule: each change should finish one coherent domain boundary: define the owner
service, migrate the highest-risk callers, and add focused tests. Avoid broad
mechanical rewrites that obscure business behavior.

## Dotmac CRM Application Retirement

The complete migration contract is
`docs/designs/CRM_WEB_RETIREMENT.md`. Its executable route-level control is
`docs/audits/crm_web_retirement_ledger.json`, generated and validated by
`scripts/architecture/crm_web_retirement.py`.

Every operational capability exposed by every CRM web module is in scope. The
migration closes capability, usable-surface, data, caller, job, traffic,
fallback, and source-deletion obligations before CRM is decommissioned.
Initial “covered”, “partial”, “owner/policy”, “surface gap”, and
“replacement/retirement” classifications are discovery states, not permission
to omit a module.

Each CRM capability migrates to its actual domain owner in this registry; there
is no omnibus CRM-retirement service that becomes a parallel business owner.
Routes and templates are adapters, retained CRM identifiers are provenance,
and external CRM integration remains transport/observation only. A route is
retired only after its replacement or explicit removal is reviewed, parity and
control evidence pass, data and callers are migrated, shadow/cutover and
rollback gates are satisfied, fallbacks are removed, a defined observation
window shows zero traffic, and the CRM source is deleted.

Temporary exception accepted 2026-07-27: ADR 0006 assigns customer and reseller
portal live-chat transport and operational inbox authority back to CRM until
the explicit CRM-exit gate passes. `control.settings_spec` owns the
`comms.chat_session_authority` selection. In `crm` mode, Sub authenticates the
portal principal and invokes the versioned `crm.chat_session.v1` capability;
the browser then communicates directly with CRM. Sub does not mirror the
conversation, and the native visitor-message owner fails closed. Existing
Selfcare-only history is reconciled through the bounded idempotent import in
`docs/runbooks/TEMPORARY_CRM_CHAT_AUTHORITY.md`. Final cutover sets the control
back to `selfcare`, verifies zero CRM portal traffic, disables the temporary
capability, and retires the exception.

The service-team boundary preserves Sub identity: `party.registry` remains the
Person Party owner and `auth.staff_provisioning` remains the staff-principal
and RBAC owner. `operations.service_team_lifecycle` owns only stable team
identity and Party-backed membership. `operations.service_team_composition`
owns governed capability bindings, many member responsibilities, typed
GeoArea/global scopes, explicit team relationships, provider-neutral external
observations, and exact domain routing policies. The one-time
`operations.service_team_source_retirement` gate verifies the five retained
native pointers and retires workflow-setting sources without CRM identity or
membership adoption. Legacy scalar type, region, manager, role, and workforce
columns remain migration shadows until the complete-cohort comparison and
explicit GeoArea review are clean.

The reviewed Sub target is
`9a09d8d0e293d0f6424eee5f90d2f69ff7f1fa2a` (`v7.33.0`). Merged PRs
#1601 through #1611 are included in the target assessment; unpushed local
worktrees are not evidence.

Collaboration-quality documentation is part of the source-of-truth contract.
Current architecture documents, migration descriptions, code comments, and
operator guidance must name the owner, affected capability, compatibility
boundary, verification gate, and current state in terms another team can act
on. Do not rely on unexplained internal sequence labels, pull-request numbers,
or shorthand such as “phase N” or “slice N” as the explanation. Historical
plans may preserve chronology, but any rule or contract promoted from them must
be restated in durable domain language here or in the owning design document.

Architecture liveness is checked in both directions. Every declared owner must
have a real application/operator caller, and every new service module with a
persistence-like mutation must name a declared owner. The 229 existing
undeclared writer-like modules are an explicit shrink-only migration baseline,
not approved parallel writers; resolving an owner or removing its write requires
deleting the baseline entry. Adding an entry requires an explicit ownership
review.

## Sales-to-Service Lifecycle

The complete contract is
`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`. Sub owns the chain from a
signed/staff interaction through immutable Lead origin, exact Party/account
conversion, Quote, SalesOrder, Project, vendor implementation, ServiceOrder,
Subscription activation, CX acceptance, and later support history.

New writes use structural foreign keys. Provider and legacy IDs remain
provenance. Part payment cannot create service; vendor verification is the
implementation release decision; a successful provisioning result is the only
path that may activate a sales-created Subscription; CX acceptance is a
separate append-only actor/time/event decision.

Cross-owner consequences run only after their source fact commits. Each owner
stages its output event atomically with its transition; the registered
sales-lifecycle projection handler consumes the funding-satisfied,
vendor-verification, service-order-release, service-order-completion, and
CX-acceptance outbox events, then requests idempotent work from the next
owner. Originating owners never write downstream roots, a consequence that
cannot be applied stays a failed retryable delivery rather than a warning
log, and replayable projection failure cannot roll back already-authoritative
evidence.

Operational defaults resolve from domain settings and connector configuration.
Checked-in enums, capability/event names, legal transition edges, idempotency
formats, and policy versions are versioned protocol contracts. HTTP remains an
adapter: lifecycle owners return transport-neutral errors and never import or
raise HTTP types.

The reverse-liveness burn-down names `observability.audit_log` as the canonical
audit-event writer, `control.settings_bootstrap` as the startup
default-materialization owner, and `secrets.settings_migration` as the live
OpenBao settings migration boundary. Bootstrap writes defaults through
`control.domain_settings`; it does not create a second runtime settings writer.

<!-- BEGIN GENERATED SOT MANIFEST -->
## Contracted Ownership Manifest

This section is generated from the typed contracts in
`app/services/sot_relationships.py`. Edit the registry and regenerate;
do not hand-edit these rows.

| Service | Concern | Role | Authoritative inputs | Transaction | Migration | Steward | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `customer.crm_subscriber_provisioning` | authenticated CRM Subscriber provisioning coordination | `application_coordinator` | authenticated CRM provisioning command evidence ← `customer.crm_subscriber_provisioning`<br>retained exact CRM Subscriber provenance ← `customer.crm_subscriber_provisioning`<br>canonical Subscriber account state ← `customer.accounts` | `coordinator_managed` | `cutover_ready` | customer operations | `docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`tests/test_crm_subscriber_provisioning.py`<br>`tests/test_crm_api.py`<br>`tests/architecture/test_crm_customer_boundary.py` |
| `customer.billing_approval` | atomic account billing-approval and lifecycle transition | `application_coordinator` | account billing-approval command evidence ← `customer.billing_approval`<br>canonical account billing-approval fact ← `customer.billing_approval`<br>canonical account lifecycle state ← `access.subscription_lifecycle`<br>canonical subscription lifecycle state ← `access.subscription_lifecycle` | `coordinator_managed` | `complete` | customer and billing operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/adr/0003-permanent-customer-financial-lifecycle.md`<br>`tests/test_account_billing_approval.py`<br>`tests/architecture/test_account_billing_approval_boundary.py` |
| `customer.billing_approval` | account billing-approval drift reconciliation | `application_coordinator` | account billing-approval command evidence ← `customer.billing_approval`<br>canonical account billing-approval fact ← `customer.billing_approval`<br>canonical account lifecycle state ← `access.subscription_lifecycle`<br>canonical subscription lifecycle state ← `access.subscription_lifecycle`<br>effective subscription billing treatment ← `financial.subscription_billing_treatments` | `coordinator_managed` | `complete` | customer and billing operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/adr/0003-permanent-customer-financial-lifecycle.md`<br>`tests/test_account_billing_approval.py`<br>`tests/architecture/test_account_billing_approval_boundary.py` |
| `customer.name_remediation` | July 20 CRM name remediation manifest execution | `command_writer` | CRM identity-change audit evidence ← `observability.audit_log`<br>legacy Subscriber name state ← `customer.accounts` | `owner_managed` | `complete` | customer operations | `docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`tests/test_crm_customer_name_repair.py` |
| `customer.name_remediation` | PII-free CRM name repair manifest generation | `command_writer` | CRM identity-change audit evidence ← `observability.audit_log`<br>legacy Subscriber name state ← `customer.accounts` | `owner_managed` | `complete` | customer operations | `docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`tests/test_crm_customer_name_repair.py` |
| `customer.name_repairs` | evidence-bound legacy Subscriber name repair | `command_writer` | approved customer-name repair manifest ← `customer.name_repairs`<br>canonical legacy Subscriber name state ← `customer.accounts`<br>immutable CRM overwrite audit evidence ← `observability.audit_log`<br>canonical Party identity binding ← `party.registry` | `owner_managed` | `complete` | customer operations | `docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_restore_crm_placeholder_identity.py`<br>`tests/architecture/test_crm_customer_boundary.py` |
| `customer.financial_position` | distinct invoice-receivable and prepaid-funding summaries | `resolver` | reviewed prepaid opening position ← `financial.prepaid_funding_reconstruction`<br>canonical payment and refund documents ← `financial.payments`<br>canonical collectible invoice documents ← `financial.invoices`<br>canonical paid prepaid consumption documents ← `financial.invoices`<br>canonical renewal debit evidence ← `financial.ledger`<br>canonical credit and adjustment evidence ← `financial.ledger` | `read_only` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`tests/test_customer_financial_ledger.py`<br>`tests/architecture/test_prepaid_funding_reconstruction_ownership.py` |
| `customer.financial_position` | customer-visible financial position | `resolver` | reviewed prepaid opening position ← `financial.prepaid_funding_reconstruction`<br>canonical payment and refund documents ← `financial.payments`<br>canonical collectible invoice documents ← `financial.invoices`<br>canonical paid prepaid consumption documents ← `financial.invoices`<br>canonical renewal debit evidence ← `financial.ledger`<br>canonical credit and adjustment evidence ← `financial.ledger` | `read_only` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`tests/test_customer_financial_ledger.py`<br>`tests/architecture/test_prepaid_funding_reconstruction_ownership.py` |
| `customer.financial_position` | bounded cohort financial projections | `resolver` | reviewed prepaid opening position ← `financial.prepaid_funding_reconstruction`<br>canonical payment and refund documents ← `financial.payments`<br>canonical collectible invoice documents ← `financial.invoices`<br>canonical paid prepaid consumption documents ← `financial.invoices`<br>canonical renewal debit evidence ← `financial.ledger`<br>canonical credit and adjustment evidence ← `financial.ledger` | `read_only` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`tests/test_customer_financial_ledger.py`<br>`tests/architecture/test_prepaid_funding_reconstruction_ownership.py` |
| `customer.financial_position` | currency-typed complete billing headline projection | `resolver` | canonical payment and refund documents ← `financial.payments`<br>canonical collectible invoice documents ← `financial.invoices`<br>canonical paid prepaid consumption documents ← `financial.invoices`<br>canonical credit and adjustment evidence ← `financial.ledger` | `read_only` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`tests/test_customer_financial_ledger.py`<br>`tests/architecture/test_prepaid_funding_reconstruction_ownership.py` |
| `customer.account_status_actions` | administrative account-status impact preview | `resolver` | authenticated administrative status context ← `customer.identity_scope`<br>canonical account and subscription lifecycle state ← `access.subscription_lifecycle`<br>account-status action protocol ← `customer.account_status_actions` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_account_status_commands.py`<br>`tests/architecture/test_generic_lifecycle_edit_boundary.py` |
| `customer.account_status_actions` | administrative account-bound idempotent status confirmation | `application_coordinator` | authenticated administrative status context ← `customer.identity_scope`<br>canonical account and subscription lifecycle state ← `access.subscription_lifecycle`<br>signed account-status preview evidence ← `customer.account_status_actions`<br>account-bound status idempotency evidence ← `customer.account_status_actions`<br>account-status action protocol ← `customer.account_status_actions` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_account_status_commands.py`<br>`tests/architecture/test_generic_lifecycle_edit_boundary.py` |
| `customer.reseller_status_actions` | reseller-scoped account-action impact preview | `resolver` | canonical reseller account scope ← `customer.identity_scope`<br>canonical account and subscription lifecycle state ← `access.subscription_lifecycle`<br>reseller account-status action protocol ← `customer.reseller_status_actions` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_reseller_gaps.py`<br>`tests/test_reseller_portal_services.py`<br>`tests/architecture/test_reseller_status_action_boundary.py` |
| `customer.reseller_status_actions` | lock-aware account-action eligibility | `policy` | canonical account and subscription lifecycle state ← `access.subscription_lifecycle`<br>canonical enforcement lock and login-conflict state ← `access.subscription_lifecycle`<br>reseller account-status action protocol ← `customer.reseller_status_actions` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_reseller_gaps.py`<br>`tests/test_reseller_portal_services.py`<br>`tests/architecture/test_reseller_status_action_boundary.py` |
| `customer.reseller_status_actions` | account-action stale-preview fingerprint | `resolver` | canonical reseller account scope ← `customer.identity_scope`<br>canonical account and subscription lifecycle state ← `access.subscription_lifecycle`<br>canonical enforcement lock and login-conflict state ← `access.subscription_lifecycle`<br>reseller account-status action protocol ← `customer.reseller_status_actions` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_reseller_gaps.py`<br>`tests/test_reseller_portal_services.py`<br>`tests/architecture/test_reseller_status_action_boundary.py` |
| `customer.reseller_status_actions` | account-bound idempotent status confirmation | `application_coordinator` | authenticated reseller status command context ← `customer.identity_scope`<br>canonical reseller account scope ← `customer.identity_scope`<br>canonical account and subscription lifecycle state ← `access.subscription_lifecycle`<br>canonical enforcement lock and login-conflict state ← `access.subscription_lifecycle`<br>signed status preview evidence ← `customer.reseller_status_actions`<br>account-bound status idempotency evidence ← `customer.reseller_status_actions`<br>reseller account-status action protocol ← `customer.reseller_status_actions` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_reseller_gaps.py`<br>`tests/test_reseller_portal_services.py`<br>`tests/architecture/test_reseller_status_action_boundary.py` |
| `customer.field_job_chat` | subscriber-scoped job chat read and send | `transport` | authenticated subscriber identity ← `customer.identity_scope`<br>canonical job chat conversation ← `communications.team_inbox_field_job`<br>canonical work order ownership ← `operations.work_orders` | `not_applicable` | `native` | customer experience platform | `docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_field_job_chat.py` |
| `billing.addon_contract_backfill` | recurring add-on contract migration snapshot | `observation_collector` | legacy recurring add-on facts ← `financial.addon_purchases`<br>recorded billing contract boundary ← `billing.contracts` | `owner_managed` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_addon_contract_backfill.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `billing.contracts` | versioned billing contract terms | `authoritative_record` | accepted commercial order line ← `sales.orders`<br>canonical subscription projection ← `access.subscription_lifecycle`<br>effective tax treatment inputs ← `financial.tax_configuration`<br>recurring add-on migration output ← `billing.addon_contract_backfill`<br>live recurring add-on purchase output ← `financial.addon_purchases`<br>recorded billing contract terms ← `billing.contracts`<br>receipted owner-output deliveries ← `events.owner_outputs`<br>exact pending-terms time trigger ← `runtime.durable_timers` | `owner_managed` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_contracts.py`<br>`tests/test_billing_addon_contract_backfill.py`<br>`tests/test_api_me_addons.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `billing.contracts` | billing contract version supersession | `command_writer` | recorded billing contract terms ← `billing.contracts`<br>exact pending-terms time trigger ← `runtime.durable_timers`<br>receipted owner-output deliveries ← `events.owner_outputs` | `owner_managed` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_contracts.py`<br>`tests/test_billing_addon_contract_backfill.py`<br>`tests/test_api_me_addons.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `billing.contracts` | effective billing contract resolution | `resolver` | recorded billing contract terms ← `billing.contracts` | `owner_managed` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_contracts.py`<br>`tests/test_billing_addon_contract_backfill.py`<br>`tests/test_api_me_addons.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `billing.obligations` | unique billing obligation identity | `authoritative_record` | recorded billing contract terms ← `billing.contracts`<br>recorded billing obligations ← `billing.obligations`<br>deterministic target rating ← `billing.rating`<br>receipted owner-output deliveries ← `events.owner_outputs` | `owner_managed` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_obligations.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `billing.obligations` | immutable obligation rating provenance | `authoritative_record` | recorded billing contract terms ← `billing.contracts`<br>deterministic target rating ← `billing.rating`<br>recorded billing obligations ← `billing.obligations` | `owner_managed` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_obligations.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `billing.obligations` | billing obligation state transition | `command_writer` | recorded billing obligations ← `billing.obligations` | `owner_managed` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_obligations.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `billing.shadow_verification` | shadow pipeline delivery evidence | `authoritative_record` | terminal shadow obligation output ← `billing.obligations`<br>receipted owner-output deliveries ← `events.owner_outputs`<br>recorded shadow verification evidence ← `billing.shadow_verification` | `owner_managed` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_shadow_pipeline.py`<br>`tests/test_billing_phase2_shadow.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `billing.shadow_verification` | phase cutover verification evidence | `authoritative_record` | complete active subscription cohort ← `access.subscription_lifecycle`<br>recorded billing contract terms ← `billing.contracts`<br>recorded billing obligations ← `billing.obligations`<br>deterministic target rating ← `billing.rating`<br>current postpaid billing preview ← `financial.billing_automation`<br>current prepaid renewal preview ← `financial.prepaid_service_renewals`<br>receipted owner-output deliveries ← `events.owner_outputs`<br>recorded shadow verification evidence ← `billing.shadow_verification` | `owner_managed` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_shadow_pipeline.py`<br>`tests/test_billing_phase2_shadow.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `billing.rating` | deterministic obligation rating | `resolver` | recorded billing contract terms ← `billing.contracts`<br>effective tax treatment inputs ← `financial.tax_configuration` | `read_only` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_rating.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `financial.customer_subledger` | append-only customer posting groups | `authoritative_record` | deciding owner command evidence ← `customer.accounts`<br>recorded customer postings ← `financial.customer_subledger` | `participant` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_customer_subledger.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `financial.customer_subledger` | customer posting reversal chain | `command_writer` | recorded customer postings ← `financial.customer_subledger` | `participant` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_customer_subledger.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `financial.customer_subledger` | typed per-currency subledger position | `resolver` | recorded customer postings ← `financial.customer_subledger` | `participant` | `shadowing` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_customer_subledger.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `runtime.durable_timers` | owner-bound durable timer generations | `authoritative_record` | owning transition command evidence ← `events.dispatcher`<br>recorded durable timers ← `runtime.durable_timers` | `owner_managed` | `shadowing` | platform and billing operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_durable_timers.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `runtime.durable_timers` | due-timer trigger emission | `command_writer` | recorded durable timers ← `runtime.durable_timers` | `owner_managed` | `shadowing` | platform and billing operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_durable_timers.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `collections.postpaid_policy` | typed overdue-receivable decision | `policy` | recorded billing obligations ← `billing.obligations` | `read_only` | `shadowing` | billing operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_collections_target_lifecycle.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `collections.prepaid_policy` | typed uncovered-service decision | `policy` | recorded billing obligations ← `billing.obligations`<br>typed per-currency subledger position ← `financial.customer_subledger` | `read_only` | `shadowing` | billing operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_collections_target_lifecycle.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `collections.lifecycle` | reason-scoped collections case workflow | `authoritative_record` | typed mode-policy proposals ← `collections.postpaid_policy`<br>recorded collections cases ← `collections.lifecycle` | `owner_managed` | `shadowing` | billing operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_collections_target_lifecycle.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `collections.lifecycle` | collections case close and reopen evidence | `command_writer` | recorded collections cases ← `collections.lifecycle` | `owner_managed` | `shadowing` | billing operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_collections_target_lifecycle.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `sales.order_funding` | finite order-obligation funding set | `authoritative_record` | exact obligation resolution outputs ← `billing.obligations`<br>recorded funding gates ← `sales.order_funding` | `owner_managed` | `shadowing` | sales and billing operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_sales_order_funding.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `sales.order_funding` | exact funding-gate transition evidence | `command_writer` | exact obligation resolution outputs ← `billing.obligations`<br>recorded funding gates ← `sales.order_funding` | `owner_managed` | `shadowing` | sales and billing operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_sales_order_funding.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `integration.dotmac_erp_billing_adapter` | versioned ERP billing payload staging | `authoritative_record` | committed billing owner outputs ← `events.owner_outputs`<br>recorded ERP exports ← `integration.dotmac_erp_billing_adapter` | `owner_managed` | `shadowing` | finance and platform operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_erp_billing_adapter.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `integration.dotmac_erp_billing_adapter` | durable ERP delivery and acknowledgement evidence | `command_writer` | recorded ERP exports ← `integration.dotmac_erp_billing_adapter` | `owner_managed` | `shadowing` | finance and platform operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_erp_billing_adapter.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `financial.account_adjustments` | prepaid account-debit eligibility and preview | `policy` | canonical Subscriber account state ← `customer.accounts`<br>canonical append-only ledger state ← `financial.ledger`<br>resolved customer financial position ← `customer.financial_position`<br>billing default-currency setting ← `control.settings_spec` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`docs/audits/BILLING_ALIGNMENT_RUN_2026-07-12.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_account_adjustment_evidence.py`<br>`tests/architecture/test_account_adjustment_boundary.py`<br>`tests/architecture/test_financial_action_boundaries.py`<br>`tests/architecture/test_financial_ownership.py` |
| `financial.account_adjustments` | locked account-debit confirmation | `command_writer` | account-adjustment command evidence ← `financial.account_adjustments`<br>canonical Subscriber account state ← `customer.accounts`<br>canonical append-only ledger state ← `financial.ledger`<br>resolved customer financial position ← `customer.financial_position`<br>billing default-currency setting ← `control.settings_spec` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`docs/audits/BILLING_ALIGNMENT_RUN_2026-07-12.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_account_adjustment_evidence.py`<br>`tests/architecture/test_account_adjustment_boundary.py`<br>`tests/architecture/test_financial_action_boundaries.py`<br>`tests/architecture/test_financial_ownership.py` |
| `financial.account_adjustments` | account-adjustment idempotency and audit evidence | `authoritative_record` | account-adjustment command evidence ← `financial.account_adjustments`<br>canonical Subscriber account state ← `customer.accounts`<br>canonical append-only ledger state ← `financial.ledger` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`docs/audits/BILLING_ALIGNMENT_RUN_2026-07-12.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_account_adjustment_evidence.py`<br>`tests/architecture/test_account_adjustment_boundary.py`<br>`tests/architecture/test_financial_action_boundaries.py`<br>`tests/architecture/test_financial_ownership.py` |
| `financial.account_adjustments` | exact account-adjustment ledger links | `authoritative_record` | account-adjustment command evidence ← `financial.account_adjustments`<br>canonical append-only ledger state ← `financial.ledger` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`docs/audits/BILLING_ALIGNMENT_RUN_2026-07-12.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_account_adjustment_evidence.py`<br>`tests/architecture/test_account_adjustment_boundary.py`<br>`tests/architecture/test_financial_action_boundaries.py`<br>`tests/architecture/test_financial_ownership.py` |
| `financial.account_adjustments` | previewed account-adjustment reversal evidence | `command_writer` | account-adjustment command evidence ← `financial.account_adjustments`<br>canonical Subscriber account state ← `customer.accounts`<br>canonical append-only ledger state ← `financial.ledger`<br>resolved customer financial position ← `customer.financial_position` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`docs/audits/BILLING_ALIGNMENT_RUN_2026-07-12.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_account_adjustment_evidence.py`<br>`tests/architecture/test_account_adjustment_boundary.py`<br>`tests/architecture/test_financial_action_boundaries.py`<br>`tests/architecture/test_financial_ownership.py` |
| `financial.topup_intents` | direct bank-transfer availability and configured-account projection | `policy` | canonical direct-transfer bank destinations ← `financial.collection_accounts`<br>canonical direct-transfer customer instructions ← `control.settings_spec` | `participant` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_direct_transfer_intents.py`<br>`tests/test_topup_intent_projection.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_topup_intent_status.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.topup_intents` | invoice direct-transfer intent record creation and replacement | `command_writer` | canonical direct-transfer top-up intent ← `financial.topup_intents`<br>direct-transfer creation command evidence ← `financial.direct_transfer_intent_commands`<br>top-up intent transition protocol ← `financial.topup_intents` | `participant` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_direct_transfer_intents.py`<br>`tests/test_topup_intent_projection.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_topup_intent_status.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.topup_intents` | direct-transfer top-up intent proof submission transition | `command_writer` | canonical direct-transfer top-up intent ← `financial.topup_intents`<br>direct-transfer proof-link command evidence ← `financial.topup_intents`<br>top-up intent transition protocol ← `financial.topup_intents` | `participant` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_direct_transfer_intents.py`<br>`tests/test_topup_intent_projection.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_topup_intent_status.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.topup_intents` | direct-transfer reviewed-proof resolution projection | `projection_writer` | canonical direct-transfer top-up intent ← `financial.topup_intents`<br>typed reviewed-proof resolution evidence ← `financial.topup_intents`<br>canonical succeeded payment evidence ← `financial.payments`<br>top-up intent transition protocol ← `financial.topup_intents` | `participant` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_direct_transfer_intents.py`<br>`tests/test_topup_intent_projection.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_topup_intent_status.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.topup_intents` | gateway invoice and reseller checkout intent record creation | `command_writer` | canonical gateway checkout creation evidence ← `financial.gateway_topup_intent_commands`<br>top-up intent transition protocol ← `financial.topup_intents` | `participant` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_direct_transfer_intents.py`<br>`tests/test_topup_intent_projection.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_topup_intent_status.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.topup_intents` | saved-card top-up intent failure projection | `command_writer` | canonical top-up intent projection target ← `financial.topup_intents`<br>typed saved-card failure evidence ← `financial.gateway_topup_intent_commands`<br>top-up intent transition protocol ← `financial.topup_intents` | `participant` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_direct_transfer_intents.py`<br>`tests/test_topup_intent_projection.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_topup_intent_status.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.topup_intents` | top-up intent completed-payment projection | `command_writer` | canonical top-up intent projection target ← `financial.topup_intents`<br>canonical succeeded payment evidence ← `financial.payments`<br>typed top-up intent completion evidence ← `financial.topup_intents` | `participant` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_direct_transfer_intents.py`<br>`tests/test_topup_intent_projection.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_topup_intent_status.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.topup_intents` | gateway top-up intent expiry decision | `command_writer` | canonical top-up intent projection target ← `financial.topup_intents`<br>canonical top-up reconciliation expiry policy ← `control.settings_spec`<br>typed gateway expiry observation ← `external:payment_provider` | `participant` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_direct_transfer_intents.py`<br>`tests/test_topup_intent_projection.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_topup_intent_status.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.direct_transfer_intent_commands` | customer direct-transfer intent creation coordination | `application_coordinator` | authenticated direct-transfer creation command ← `financial.direct_transfer_intent_commands`<br>canonical customer account ← `customer.accounts`<br>canonical payable invoice ← `financial.invoices`<br>canonical customer WHT policy ← `financial.customer_tax_policies`<br>canonical direct-transfer configuration ← `financial.topup_intents`<br>canonical direct-transfer lifetime and amount policy ← `control.settings_spec`<br>canonical deposit intent protocol ← `financial.account_credit_deposits`<br>canonical invoice direct-transfer intent protocol ← `financial.topup_intents` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_direct_transfer_intents.py`<br>`tests/test_customer_portal_topup_flow.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.topup_intent_proof_reconciliation` | submitted intent terminal-proof reconciliation | `reconciler` | canonical payment-proof review evidence ← `financial.payment_proofs`<br>canonical direct-transfer top-up intent ← `financial.topup_intents`<br>canonical succeeded payment evidence ← `financial.payments`<br>canonical reviewed-proof intent projection protocol ← `financial.topup_intents` | `owner_managed` | `cut_over` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/ACCOUNT_CREDIT_DEPOSITS.md`<br>`tests/test_topup_intent_proof_reconciliation.py`<br>`tests/test_payment_proofs.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.customer_tax_policies` | customer withholding-tax eligibility policy | `command_writer` | customer WHT policy command context ← `financial.customer_tax_policies`<br>canonical customer account ← `customer.accounts`<br>canonical customer WHT policy record ← `financial.customer_tax_policies` | `owner_managed` | `native` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FRONTEND_SPEC.md`<br>`docs/ACCOUNT_CREDIT_DEPOSITS.md`<br>`tests/test_subscriber_billing_config.py`<br>`tests/test_direct_transfer_intents.py`<br>`tests/test_customer_wht_policy_migration.py` |
| `financial.gateway_topup_intent_commands` | customer gateway top-up intent creation coordination | `application_coordinator` | authenticated customer gateway creation command ← `financial.gateway_topup_intent_commands`<br>canonical payable invoice ← `financial.invoices`<br>canonical gateway lifetime and amount policy ← `control.settings_spec`<br>canonical deposit intent protocol ← `financial.account_credit_deposits`<br>canonical gateway intent protocol ← `financial.topup_intents`<br>enabled checkout capability binding ← `integration.installations` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_gateway_topup_intents.py`<br>`tests/test_customer_portal_topup_flow.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.gateway_topup_intent_commands` | reseller gateway top-up intent creation coordination | `application_coordinator` | authenticated reseller gateway creation command ← `financial.gateway_topup_intent_commands`<br>canonical reseller billing account ← `financial.billing_accounts`<br>canonical gateway lifetime and amount policy ← `control.settings_spec`<br>canonical gateway intent protocol ← `financial.topup_intents`<br>enabled checkout capability binding ← `integration.installations` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_gateway_topup_intents.py`<br>`tests/test_customer_portal_topup_flow.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.gateway_topup_intent_commands` | saved-card charge failure coordination | `application_coordinator` | typed saved-card failure command ← `financial.gateway_topup_intent_commands`<br>canonical gateway intent protocol ← `financial.topup_intents`<br>canonical saved-card retry reservation ← `financial.gateway_topup_intent_commands` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_gateway_topup_intents.py`<br>`tests/test_customer_portal_topup_flow.py`<br>`tests/architecture/test_topup_intent_ownership.py` |
| `financial.account_credit_deposits` | Deposit Account Credit eligibility and preview | `policy` | canonical deposit customer account ← `customer.accounts`<br>canonical payable invoice set ← `financial.invoices`<br>canonical payment-backed account credit ← `financial.payments`<br>canonical deposit eligibility policy ← `financial.account_credit_deposits` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/ACCOUNT_CREDIT_DEPOSITS.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_account_credit_deposits.py`<br>`tests/test_customer_portal_topup_flow.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_payment_proofs.py`<br>`tests/architecture/test_account_credit_deposit_ownership.py` |
| `financial.account_credit_deposits` | typed deposit intent lifecycle and provider correlation | `command_writer` | typed deposit intent creation evidence ← `financial.account_credit_deposits`<br>canonical deposit customer account ← `customer.accounts`<br>canonical deposit eligibility policy ← `financial.account_credit_deposits` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/ACCOUNT_CREDIT_DEPOSITS.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_account_credit_deposits.py`<br>`tests/test_customer_portal_topup_flow.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_payment_proofs.py`<br>`tests/architecture/test_account_credit_deposit_ownership.py` |
| `financial.account_credit_deposits` | verified Deposit Account Credit settlement command | `command_writer` | typed verified deposit settlement evidence ← `financial.account_credit_deposits`<br>canonical typed deposit intent ← `financial.account_credit_deposits`<br>canonical subscriber payment settlement protocol ← `financial.payments`<br>canonical account-credit application protocol ← `financial.account_credit_applications`<br>canonical top-up intent completion protocol ← `financial.topup_intents` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/ACCOUNT_CREDIT_DEPOSITS.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_account_credit_deposits.py`<br>`tests/test_customer_portal_topup_flow.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_payment_proofs.py`<br>`tests/architecture/test_account_credit_deposit_ownership.py` |
| `financial.account_credit_deposits` | deposit-to-payment evidence link | `authoritative_record` | canonical typed deposit intent ← `financial.account_credit_deposits`<br>canonical subscriber payment settlement protocol ← `financial.payments` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/ACCOUNT_CREDIT_DEPOSITS.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_account_credit_deposits.py`<br>`tests/test_customer_portal_topup_flow.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_payment_proofs.py`<br>`tests/architecture/test_account_credit_deposit_ownership.py` |
| `financial.account_credit_deposits` | post-application funding-change outbox event | `event_policy` | typed verified deposit settlement evidence ← `financial.account_credit_deposits`<br>canonical typed deposit intent ← `financial.account_credit_deposits`<br>canonical subscriber payment settlement protocol ← `financial.payments` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/ACCOUNT_CREDIT_DEPOSITS.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_account_credit_deposits.py`<br>`tests/test_customer_portal_topup_flow.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_payment_proofs.py`<br>`tests/architecture/test_account_credit_deposit_ownership.py` |
| `financial.payment_configuration_staff_actions` | reviewed payment configuration lifecycle and audit coordination | `application_coordinator` | payment configuration staff command ← `financial.payment_configuration_staff_actions`<br>canonical collection-account state ← `financial.collection_accounts`<br>canonical settlement-attribution state ← `financial.payment_configuration_staff_actions` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/PAYMENT_CONFIGURATION_SETTINGS_SAFE_ACTIONS.md`<br>`tests/test_payment_configuration_staff_actions.py`<br>`tests/test_payment_configuration_settings_ui.py` |
| `financial.payment_routing` | installation-backed customer gateway eligibility | `resolver` | enabled payment capability installation bundle ← `integration.installations`<br>canonical gateway finance identity ← `financial.payment_gateway_finance` | `read_only` | `complete` | finance operations | `docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_payment_routing.py`<br>`tests/test_customer_portal_billing_routes.py`<br>`tests/architecture/test_payment_gateway_control_plane.py` |
| `financial.payment_routing` | ordered customer gateway presentment policy | `policy` | enabled payment capability installation bundle ← `integration.installations` | `read_only` | `complete` | finance operations | `docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_payment_routing.py`<br>`tests/test_customer_portal_billing_routes.py`<br>`tests/architecture/test_payment_gateway_control_plane.py` |
| `financial.payment_routing` | checkout provider and binding selection | `policy` | enabled payment capability installation bundle ← `integration.installations`<br>canonical gateway finance identity ← `financial.payment_gateway_finance`<br>customer checkout provider request ← `financial.payment_routing` | `read_only` | `complete` | finance operations | `docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_payment_routing.py`<br>`tests/test_customer_portal_billing_routes.py`<br>`tests/architecture/test_payment_gateway_control_plane.py` |
| `financial.payment_gateway_finance` | gateway finance provider identity bootstrap | `command_writer` | payment gateway connector manifest ← `integration.registry`<br>payment gateway installation setup ← `integration.installations` | `participant` | `complete` | finance operations | `docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_integrations_payment_gateways.py`<br>`tests/test_payment_routing.py`<br>`tests/architecture/test_sot_manifest_contracts.py` |
| `financial.payment_gateway_finance` | gateway settlement-channel bootstrap | `command_writer` | payment gateway connector manifest ← `integration.registry`<br>payment gateway installation setup ← `integration.installations` | `participant` | `complete` | finance operations | `docs/designs/PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_integrations_payment_gateways.py`<br>`tests/test_payment_routing.py`<br>`tests/architecture/test_sot_manifest_contracts.py` |
| `financial.invoice_draft_authoring` | administrative invoice draft authoring coordination | `application_coordinator` | authenticated administrative draft command ← `financial.invoice_draft_authoring`<br>canonical customer account ← `customer.accounts`<br>canonical invoice draft aggregate ← `financial.invoices`<br>canonical invoice tax rates ← `financial.tax_configuration` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/INVOICE_DRAFT_AUTHORING.md`<br>`tests/test_invoice_draft_authoring.py`<br>`tests/test_web_billing_invoice_forms.py`<br>`tests/architecture/test_invoice_draft_authoring_ownership.py` |
| `financial.payment_proofs` | payment-proof review lifecycle | `authoritative_record` | payment-proof command context ← `financial.payment_proofs`<br>submitted transfer evidence ← `external:bank-transfer-submitter`<br>canonical payment-proof record ← `financial.payment_proofs`<br>payment-proof lifecycle protocol ← `financial.payment_proofs`<br>canonical direct-transfer top-up intent protocol ← `financial.topup_intents` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/test_payment_proof_reviewer_notifications.py`<br>`tests/test_reseller_proof_double_credit.py`<br>`tests/test_payment_proof_admin_routes.py`<br>`tests/architecture/test_payment_proof_reviewer_notification_ownership.py` |
| `financial.payment_proofs` | proof-backed payment request | `command_writer` | payment-proof command context ← `financial.payment_proofs`<br>canonical payment-proof record ← `financial.payment_proofs`<br>canonical subscriber account target ← `customer.accounts`<br>canonical reseller billing-account target ← `financial.billing_accounts`<br>canonical subscriber payment settlement protocol ← `financial.payments`<br>canonical consolidated settlement protocol ← `financial.consolidated_payments`<br>canonical deposit intent evidence ← `financial.account_credit_deposits`<br>canonical withholding-tax recognition protocol ← `financial.tax_accounting` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/test_payment_proof_reviewer_notifications.py`<br>`tests/test_reseller_proof_double_credit.py`<br>`tests/test_payment_proof_admin_routes.py`<br>`tests/architecture/test_payment_proof_reviewer_notification_ownership.py` |
| `financial.payment_proofs` | payment-proof reviewer notification request lifecycle | `event_policy` | canonical payment-proof record ← `financial.payment_proofs`<br>canonical proof-review audience ← `auth.permission_gate`<br>payment-proof lifecycle protocol ← `financial.payment_proofs` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_proofs.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/test_payment_proof_reviewer_notifications.py`<br>`tests/test_reseller_proof_double_credit.py`<br>`tests/test_payment_proof_admin_routes.py`<br>`tests/architecture/test_payment_proof_reviewer_notification_ownership.py` |
| `financial.tax_accounting` | tax report semantics | `policy` | canonical invoice tax source documents ← `financial.invoices`<br>canonical credit-note tax source documents ← `financial.credit_notes`<br>canonical WHT source records ← `financial.tax_accounting`<br>canonical tax-application configuration ← `financial.tax_configuration` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_tax_accounting.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/integration/test_tax_accounting_concurrency.py`<br>`tests/architecture/test_tax_accounting_ownership.py` |
| `financial.tax_accounting` | output-tax invoice projection | `resolver` | canonical invoice tax source documents ← `financial.invoices`<br>canonical tax-application configuration ← `financial.tax_configuration` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_tax_accounting.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/integration/test_tax_accounting_concurrency.py`<br>`tests/architecture/test_tax_accounting_ownership.py` |
| `financial.tax_accounting` | withholding-tax receivable projection | `resolver` | canonical WHT source records ← `financial.tax_accounting` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_tax_accounting.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/integration/test_tax_accounting_concurrency.py`<br>`tests/architecture/test_tax_accounting_ownership.py` |
| `financial.tax_accounting` | tax report period and currency aggregation | `resolver` | typed tax report filter ← `financial.tax_accounting`<br>canonical invoice tax source documents ← `financial.invoices`<br>canonical credit-note tax source documents ← `financial.credit_notes`<br>canonical WHT source records ← `financial.tax_accounting` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_tax_accounting.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/integration/test_tax_accounting_concurrency.py`<br>`tests/architecture/test_tax_accounting_ownership.py` |
| `financial.tax_accounting` | credit-note tax recognition point | `policy` | canonical credit-note tax source documents ← `financial.credit_notes`<br>canonical tax-application configuration ← `financial.tax_configuration` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_tax_accounting.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/integration/test_tax_accounting_concurrency.py`<br>`tests/architecture/test_tax_accounting_ownership.py` |
| `financial.tax_accounting` | withholding-tax receivable source records | `authoritative_record` | verified proof-backed WHT evidence ← `financial.payment_proofs`<br>canonical payment settlement evidence ← `financial.payments`<br>WHT command context ← `financial.tax_accounting` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_tax_accounting.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/integration/test_tax_accounting_concurrency.py`<br>`tests/architecture/test_tax_accounting_ownership.py` |
| `financial.tax_accounting` | withholding-tax lifecycle | `command_writer` | canonical WHT source records ← `financial.tax_accounting`<br>WHT lifecycle protocol ← `financial.tax_accounting`<br>WHT command context ← `financial.tax_accounting` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_tax_accounting.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/integration/test_tax_accounting_concurrency.py`<br>`tests/architecture/test_tax_accounting_ownership.py` |
| `financial.tax_accounting` | withholding-tax official timeline | `authoritative_record` | canonical WHT source records ← `financial.tax_accounting`<br>WHT lifecycle protocol ← `financial.tax_accounting`<br>WHT command context ← `financial.tax_accounting` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_tax_accounting.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/integration/test_tax_accounting_concurrency.py`<br>`tests/architecture/test_tax_accounting_ownership.py` |
| `financial.tax_accounting` | net output-tax liability projection | `resolver` | canonical invoice tax source documents ← `financial.invoices`<br>canonical credit-note tax source documents ← `financial.credit_notes` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_tax_accounting.py`<br>`tests/test_payment_proofs_reseller_wht.py`<br>`tests/integration/test_tax_accounting_concurrency.py`<br>`tests/architecture/test_tax_accounting_ownership.py` |
| `financial.billing_profile` | prepaid/postpaid profile resolution | `resolver` | canonical account billing mode ← `customer.accounts`<br>canonical collectible subscription billing modes ← `access.subscription_lifecycle`<br>billing profile protocol ← `financial.billing_profile` | `read_only` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_billing_profile.py`<br>`tests/test_shared_policy_services.py`<br>`tests/test_billing_cleanup_remediation.py`<br>`tests/architecture/test_billing_profile_boundary.py` |
| `financial.billing_profile` | billing-mode transition policy | `policy` | canonical account billing mode ← `customer.accounts`<br>canonical collectible subscription billing modes ← `access.subscription_lifecycle`<br>canonical offer billing mode ← `service_intent.catalog_policy`<br>billing profile protocol ← `financial.billing_profile` | `read_only` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_billing_profile.py`<br>`tests/test_shared_policy_services.py`<br>`tests/test_billing_cleanup_remediation.py`<br>`tests/architecture/test_billing_profile_boundary.py` |
| `financial.prepaid_currency` | prepaid enforcement currency policy | `policy` | prepaid enforcement currency setting ← `control.settings_spec`<br>prepaid currency protocol ← `financial.prepaid_currency` | `read_only` | `complete` | billing operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_access_resolution.py`<br>`tests/test_prepaid_threshold_resolver.py`<br>`tests/architecture/test_prepaid_threshold_boundary.py` |
| `financial.subscription_billing_treatments` | subscription billing-treatment lifecycle | `command_writer` | authenticated billing-treatment command ← `auth.permission_gate`<br>canonical subscription contract ← `access.subscription_lifecycle`<br>canonical recurring service value ← `service_intent.catalog_policy`<br>canonical billing-treatment approval policy ← `control.settings_spec`<br>current billing-treatment records ← `financial.subscription_billing_treatments` | `owner_managed` | `cutover_ready` | billing and finance operations | `docs/designs/SUBSCRIPTION_BILLING_TREATMENTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_subscription_billing_treatments.py`<br>`tests/test_subscription_billing_treatment_api.py`<br>`tests/architecture/test_subscription_billing_treatment_ownership.py` |
| `financial.subscription_billing_treatments` | effective subscription customer-billing treatment | `policy` | canonical subscription contract ← `access.subscription_lifecycle`<br>canonical recurring service value ← `service_intent.catalog_policy`<br>current billing-treatment records ← `financial.subscription_billing_treatments`<br>evaluation time ← `external:system_clock` | `owner_managed` | `cutover_ready` | billing and finance operations | `docs/designs/SUBSCRIPTION_BILLING_TREATMENTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_subscription_billing_treatments.py`<br>`tests/test_subscription_billing_treatment_api.py`<br>`tests/architecture/test_subscription_billing_treatment_ownership.py` |
| `financial.subscription_billing_treatments` | billing-treatment offer and value authorization | `policy` | canonical subscription contract ← `access.subscription_lifecycle`<br>canonical recurring service value ← `service_intent.catalog_policy`<br>canonical billing-treatment approval policy ← `control.settings_spec`<br>current billing-treatment records ← `financial.subscription_billing_treatments` | `owner_managed` | `cutover_ready` | billing and finance operations | `docs/designs/SUBSCRIPTION_BILLING_TREATMENTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_subscription_billing_treatments.py`<br>`tests/test_subscription_billing_treatment_api.py`<br>`tests/architecture/test_subscription_billing_treatment_ownership.py` |
| `financial.subscription_billing_grants` | exact non-cash subscription service-period grant | `authoritative_record` | effective subscription billing treatment ← `financial.subscription_billing_treatments`<br>canonical subscription contract ← `access.subscription_lifecycle`<br>canonical recurring service value ← `service_intent.catalog_policy`<br>requested service period ← `financial.subscription_billing_grants` | `participant` | `cutover_ready` | billing and finance operations | `docs/designs/SUBSCRIPTION_BILLING_TREATMENTS.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_subscription_billing_treatments.py`<br>`tests/architecture/test_subscription_billing_treatment_ownership.py` |
| `financial.subscription_billing_grants` | non-cash grant entitlement and billing-anchor projection | `projection_writer` | exact non-cash service grant ← `financial.subscription_billing_grants`<br>canonical subscription contract ← `access.subscription_lifecycle` | `participant` | `cutover_ready` | billing and finance operations | `docs/designs/SUBSCRIPTION_BILLING_TREATMENTS.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_subscription_billing_treatments.py`<br>`tests/architecture/test_subscription_billing_treatment_ownership.py` |
| `financial.service_extensions` | service-extension lifecycle and exact grant intervals | `command_writer` | authenticated extension command ← `auth.permission_gate`<br>canonical service-extension aggregate ← `financial.service_extensions`<br>canonical subscriber scope ← `customer.accounts`<br>canonical subscription lifecycle and billing anchor ← `access.subscription_lifecycle`<br>service-extension duration policy ← `control.settings_spec`<br>reviewed historical duplicate reconciliation command ← `auth.permission_gate` | `owner_managed` | `complete` | billing and customer operations | `docs/designs/SERVICE_EXTENSION_LIFECYCLE_SOT.md`<br>`docs/designs/SERVICE_EXTENSION_EFFECTIVE_INTERVALS.md`<br>`docs/runbooks/SERVICE_EXTENSION_ACTIVITY_CUTOVER.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_service_extensions.py`<br>`tests/test_web_billing_service_extensions.py`<br>`tests/test_prepaid_service_coverage.py`<br>`tests/integration/test_service_extension_concurrency.py`<br>`tests/architecture/test_service_extension_sot_boundary.py`<br>`tests/architecture/test_service_extension_boundary.py` |
| `financial.service_extensions` | immutable applied service-extension entry evidence | `authoritative_record` | canonical service-extension aggregate ← `financial.service_extensions`<br>canonical subscription lifecycle and billing anchor ← `access.subscription_lifecycle` | `owner_managed` | `complete` | billing and customer operations | `docs/designs/SERVICE_EXTENSION_LIFECYCLE_SOT.md`<br>`docs/designs/SERVICE_EXTENSION_EFFECTIVE_INTERVALS.md`<br>`docs/runbooks/SERVICE_EXTENSION_ACTIVITY_CUTOVER.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_service_extensions.py`<br>`tests/test_web_billing_service_extensions.py`<br>`tests/test_prepaid_service_coverage.py`<br>`tests/integration/test_service_extension_concurrency.py`<br>`tests/architecture/test_service_extension_sot_boundary.py`<br>`tests/architecture/test_service_extension_boundary.py` |
| `financial.service_extensions` | service-extension billing-anchor projection | `projection_writer` | canonical service-extension aggregate ← `financial.service_extensions`<br>canonical subscription lifecycle and billing anchor ← `access.subscription_lifecycle`<br>immutable applied service-extension entry evidence ← `financial.service_extensions` | `owner_managed` | `complete` | billing and customer operations | `docs/designs/SERVICE_EXTENSION_LIFECYCLE_SOT.md`<br>`docs/designs/SERVICE_EXTENSION_EFFECTIVE_INTERVALS.md`<br>`docs/runbooks/SERVICE_EXTENSION_ACTIVITY_CUTOVER.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_service_extensions.py`<br>`tests/test_web_billing_service_extensions.py`<br>`tests/test_prepaid_service_coverage.py`<br>`tests/integration/test_service_extension_concurrency.py`<br>`tests/architecture/test_service_extension_sot_boundary.py`<br>`tests/architecture/test_service_extension_boundary.py` |
| `financial.prepaid_service_coverage` | current prepaid service coverage classification | `resolver` | canonical subscription projection ← `access.subscription_lifecycle`<br>funded service entitlement intervals ← `financial.prepaid_service_renewals`<br>non-cash grant service intervals ← `financial.subscription_billing_grants`<br>explicit granted-service intervals ← `financial.service_extensions` | `read_only` | `complete` | billing and network access | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_prepaid_service_coverage.py`<br>`tests/test_prepaid_threshold_resolver.py`<br>`tests/test_prepaid_balance_sweep.py` |
| `financial.prepaid_service_coverage` | unresolved paid-through projection classification | `resolver` | canonical subscription projection ← `access.subscription_lifecycle`<br>funded service entitlement intervals ← `financial.prepaid_service_renewals`<br>non-cash grant service intervals ← `financial.subscription_billing_grants`<br>explicit granted-service intervals ← `financial.service_extensions` | `read_only` | `complete` | billing and network access | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_prepaid_service_coverage.py`<br>`tests/test_prepaid_threshold_resolver.py`<br>`tests/test_prepaid_balance_sweep.py` |
| `financial.prepaid_service_coverage_reconciliation` | exact prepaid coverage evidence reconciliation | `reconciler` | canonical prepaid subscription and account state ← `access.subscription_lifecycle`<br>funded service entitlement intervals ← `financial.prepaid_service_renewals`<br>exact paid invoice line periods ← `financial.invoices`<br>exact prepaid renewal adjustments ← `financial.account_adjustments`<br>explicit granted-service intervals ← `financial.service_extensions` | `owner_managed` | `complete` | billing operations | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_prepaid_coverage_reconciliation.py`<br>`tests/architecture/test_prepaid_threshold_boundary.py` |
| `financial.prepaid_threshold` | prepaid enforcement threshold | `resolver` | canonical account minimum balance ← `customer.accounts`<br>prepaid default minimum setting ← `control.settings_spec`<br>canonical prepaid currency ← `financial.prepaid_currency`<br>prepaid threshold protocol ← `financial.prepaid_threshold` | `read_only` | `complete` | billing operations | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_prepaid_threshold_resolver.py`<br>`tests/test_access_resolution.py`<br>`tests/architecture/test_prepaid_threshold_boundary.py` |
| `financial.prepaid_threshold` | unfunded prepaid renewal requirement | `resolver` | canonical collectible prepaid subscriptions ← `access.subscription_lifecycle`<br>canonical current service coverage ← `financial.prepaid_service_coverage`<br>effective subscription billing treatment ← `financial.subscription_billing_treatments`<br>exact taxed contracted renewal charge ← `financial.prepaid_service_renewals`<br>canonical prepaid currency ← `financial.prepaid_currency`<br>prepaid threshold protocol ← `financial.prepaid_threshold` | `read_only` | `complete` | billing operations | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_prepaid_threshold_resolver.py`<br>`tests/test_access_resolution.py`<br>`tests/architecture/test_prepaid_threshold_boundary.py` |
| `financial.grace_policy` | account/policy/billing-default grace precedence | `policy` | canonical billing profile ← `financial.billing_profile`<br>canonical account grace configuration ← `customer.accounts`<br>canonical reseller policy assignment ← `customer.identity_scope`<br>canonical service policy assignments ← `access.subscription_lifecycle`<br>canonical policy-set configuration ← `service_intent.catalog_policy`<br>canonical grace settings ← `control.settings_spec`<br>grace policy protocol ← `financial.grace_policy` | `read_only` | `complete` | collections operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_grace_policy_sot.py`<br>`tests/test_prepaid_balance_sweep.py`<br>`tests/test_service_status.py`<br>`tests/architecture/test_grace_policy_boundary.py` |
| `financial.grace_policy` | grace provenance and deadline | `resolver` | canonical billing profile ← `financial.billing_profile`<br>canonical account grace configuration ← `customer.accounts`<br>canonical reseller policy assignment ← `customer.identity_scope`<br>canonical service policy assignments ← `access.subscription_lifecycle`<br>canonical policy-set configuration ← `service_intent.catalog_policy`<br>canonical grace settings ← `control.settings_spec`<br>grace policy protocol ← `financial.grace_policy`<br>evaluation time ← `external:system_clock` | `read_only` | `complete` | collections operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_grace_policy_sot.py`<br>`tests/test_prepaid_balance_sweep.py`<br>`tests/test_service_status.py`<br>`tests/architecture/test_grace_policy_boundary.py` |
| `financial.grace_policy` | post-grace elapsed-day decision | `policy` | grace policy protocol ← `financial.grace_policy`<br>evaluation time ← `external:system_clock` | `read_only` | `complete` | collections operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_grace_policy_sot.py`<br>`tests/test_prepaid_balance_sweep.py`<br>`tests/test_service_status.py`<br>`tests/architecture/test_grace_policy_boundary.py` |
| `financial.prepaid_enforcement` | prepaid enforcement candidate cohort | `resolver` | canonical account eligibility ← `customer.accounts`<br>canonical subscription lifecycle state ← `access.subscription_lifecycle`<br>canonical prepaid enforcement locks and timers ← `financial.prepaid_enforcement_state`<br>prepaid enforcement protocol ← `financial.prepaid_enforcement` | `read_only` | `complete` | billing operations | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_prepaid_enforcement_planner.py`<br>`tests/test_prepaid_balance_sweep.py`<br>`tests/architecture/test_prepaid_enforcement_policy_ownership.py` |
| `financial.prepaid_enforcement` | prepaid warn/suspend/restore planning | `policy` | canonical billing profile ← `financial.billing_profile`<br>canonical prepaid funding decision ← `financial.access_resolution`<br>canonical grace decision ← `financial.grace_policy`<br>canonical financial shields ← `financial.dunning`<br>canonical communication suppression ← `communications.customer_policy`<br>canonical service bundle policy ← `service_intent.catalog_policy`<br>canonical prepaid policy settings ← `control.settings_spec`<br>prepaid enforcement protocol ← `financial.prepaid_enforcement`<br>evaluation time ← `external:system_clock` | `read_only` | `complete` | billing operations | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_prepaid_enforcement_planner.py`<br>`tests/test_prepaid_balance_sweep.py`<br>`tests/architecture/test_prepaid_enforcement_policy_ownership.py` |
| `financial.prepaid_enforcement` | prepaid policy projection consumed by dry-run and execution | `resolver` | canonical prepaid policy settings ← `control.settings_spec`<br>prepaid enforcement protocol ← `financial.prepaid_enforcement`<br>evaluation time ← `external:system_clock` | `read_only` | `complete` | billing operations | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_prepaid_enforcement_planner.py`<br>`tests/test_prepaid_balance_sweep.py`<br>`tests/architecture/test_prepaid_enforcement_policy_ownership.py` |
| `financial.prepaid_enforcement_state` | prepaid low-balance timer state | `authoritative_record` | resolved prepaid enforcement transition ← `financial.prepaid_enforcement`<br>canonical prepaid enforcement timers ← `financial.prepaid_enforcement_state` | `participant` | `complete` | billing operations | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_prepaid_enforcement_state_owner.py`<br>`tests/architecture/test_prepaid_enforcement_state_boundary.py`<br>`tests/test_prepaid_balance_sweep.py`<br>`tests/test_account_lifecycle.py` |
| `financial.prepaid_enforcement_state` | prepaid deactivation timer state | `authoritative_record` | resolved prepaid enforcement transition ← `financial.prepaid_enforcement`<br>canonical prepaid enforcement timers ← `financial.prepaid_enforcement_state` | `participant` | `complete` | billing operations | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_prepaid_enforcement_state_owner.py`<br>`tests/architecture/test_prepaid_enforcement_state_boundary.py`<br>`tests/test_prepaid_balance_sweep.py`<br>`tests/test_account_lifecycle.py` |
| `financial.prepaid_enforcement_state` | funded and terminal prepaid timer cleanup | `command_writer` | resolved prepaid enforcement transition ← `financial.prepaid_enforcement`<br>resolved account lifecycle transition ← `access.subscription_lifecycle`<br>canonical prepaid enforcement timers ← `financial.prepaid_enforcement_state` | `participant` | `complete` | billing operations | `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_prepaid_enforcement_state_owner.py`<br>`tests/architecture/test_prepaid_enforcement_state_boundary.py`<br>`tests/test_prepaid_balance_sweep.py`<br>`tests/test_account_lifecycle.py` |
| `financial.prepaid_draft_reconciliation` | stranded prepaid draft classification | `resolver` | canonical prepaid draft invoice ← `financial.invoices`<br>canonical payment-backed account credit ← `financial.account_credit_applications`<br>canonical funded service entitlement ← `financial.prepaid_service_renewals`<br>canonical direct-renewal debit ← `financial.prepaid_service_renewals` | `owner_managed` | `cut_over` | billing operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/designs/PREPAID_DRAFT_RECONCILIATION.md`<br>`tests/test_prepaid_draft_reconciliation.py`<br>`tests/test_prepaid_service_renewals.py`<br>`tests/test_subscription_lifecycle_commands.py`<br>`tests/integration/test_prepaid_draft_reconciliation_concurrency.py`<br>`tests/architecture/test_prepaid_draft_reconciliation_ownership.py` |
| `financial.prepaid_draft_reconciliation` | stranded prepaid draft invoice reconciliation | `reconciler` | reviewed reconciliation command ← `financial.prepaid_draft_reconciliation`<br>canonical prepaid draft invoice ← `financial.invoices`<br>canonical payment-backed account credit ← `financial.account_credit_applications`<br>reviewed opening funding ← `financial.prepaid_funding_reconstruction`<br>canonical funded service entitlement ← `financial.prepaid_service_renewals`<br>canonical direct-renewal debit ← `financial.prepaid_service_renewals`<br>invoice and payment participant protocols ← `financial.invoices` | `owner_managed` | `cut_over` | billing operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/designs/PREPAID_DRAFT_RECONCILIATION.md`<br>`tests/test_prepaid_draft_reconciliation.py`<br>`tests/test_prepaid_service_renewals.py`<br>`tests/test_subscription_lifecycle_commands.py`<br>`tests/integration/test_prepaid_draft_reconciliation_concurrency.py`<br>`tests/architecture/test_prepaid_draft_reconciliation_ownership.py` |
| `financial.prepaid_draft_reconciliation` | reviewed opening funding invoice consumption | `reconciler` | reviewed reconciliation command ← `financial.prepaid_draft_reconciliation`<br>canonical prepaid draft invoice ← `financial.invoices`<br>canonical payment-backed account credit ← `financial.account_credit_applications`<br>reviewed opening funding ← `financial.prepaid_funding_reconstruction` | `owner_managed` | `cut_over` | billing operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/designs/PREPAID_DRAFT_RECONCILIATION.md`<br>`tests/test_prepaid_draft_reconciliation.py`<br>`tests/test_prepaid_service_renewals.py`<br>`tests/test_subscription_lifecycle_commands.py`<br>`tests/integration/test_prepaid_draft_reconciliation_concurrency.py`<br>`tests/architecture/test_prepaid_draft_reconciliation_ownership.py` |
| `financial.prepaid_draft_reconciliation` | prepaid draft reconciliation exceptions and operator alerts | `command_writer` | canonical prepaid draft invoice ← `financial.invoices`<br>canonical payment-backed account credit ← `financial.account_credit_applications`<br>reviewed opening funding ← `financial.prepaid_funding_reconstruction` | `owner_managed` | `cut_over` | billing operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/designs/PREPAID_DRAFT_RECONCILIATION.md`<br>`tests/test_prepaid_draft_reconciliation.py`<br>`tests/test_prepaid_service_renewals.py`<br>`tests/test_subscription_lifecycle_commands.py`<br>`tests/integration/test_prepaid_draft_reconciliation_concurrency.py`<br>`tests/architecture/test_prepaid_draft_reconciliation_ownership.py` |
| `financial.prepaid_recovery_billing` | suspended prepaid replacement-cycle draft creation | `application_coordinator` | locked prepaid subscription state ← `access.subscription_lifecycle`<br>active prepaid enforcement lock ← `financial.access_resolution`<br>contracted prepaid renewal price ← `financial.prepaid_service_renewals`<br>open recovery-invoice evidence ← `financial.prepaid_recovery_billing` | `coordinator_managed` | `native` | billing operations | `docs/designs/PREPAID_RECOVERY_BILLING.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_invoice_templates.py`<br>`tests/architecture/test_prepaid_recovery_billing_sot.py` |
| `financial.prepaid_recovery_billing` | full settlement and restoration of a prepaid recovery invoice | `application_coordinator` | locked recovery invoice and service scope ← `financial.prepaid_recovery_billing`<br>active prepaid enforcement lock ← `financial.access_resolution`<br>confirmed unallocated payment credit ← `financial.account_credit_applications`<br>paid-invoice entitlement protocol ← `financial.prepaid_service_renewals`<br>financial access restoration protocol ← `financial.access_resolution` | `coordinator_managed` | `native` | billing operations | `docs/designs/PREPAID_RECOVERY_BILLING.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_invoice_templates.py`<br>`tests/architecture/test_prepaid_recovery_billing_sot.py` |
| `financial.addon_purchases` | customer add-on purchase eligibility and preview | `resolver` | canonical subscription state ← `access.subscription_lifecycle`<br>offered add-on commercial terms ← `financial.addon_purchases`<br>current customer financial position ← `customer.financial_position` | `owner_managed` | `cut_over` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_api_me_addons.py`<br>`tests/test_billing_addon_contract_backfill.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `financial.addon_purchases` | add-on price and subscription-state confirmation | `policy` | canonical subscription state ← `access.subscription_lifecycle`<br>offered add-on commercial terms ← `financial.addon_purchases`<br>current customer financial position ← `customer.financial_position` | `owner_managed` | `cut_over` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_api_me_addons.py`<br>`tests/test_billing_addon_contract_backfill.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `financial.addon_purchases` | add-on purchase idempotency and audit evidence | `authoritative_record` | recorded add-on purchase evidence ← `financial.addon_purchases`<br>canonical audit participant ← `observability.audit_log` | `owner_managed` | `cut_over` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_api_me_addons.py`<br>`tests/test_billing_addon_contract_backfill.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `financial.addon_purchases` | exact add-on entitlement-to-adjustment link | `authoritative_record` | confirmed account adjustment ← `financial.account_adjustments`<br>recorded add-on purchase evidence ← `financial.addon_purchases` | `owner_managed` | `cut_over` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_api_me_addons.py`<br>`tests/test_billing_addon_contract_backfill.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `financial.addon_purchases` | canonical recurring add-on billing-terms output | `command_writer` | canonical subscription state ← `access.subscription_lifecycle`<br>offered add-on commercial terms ← `financial.addon_purchases`<br>recorded add-on purchase evidence ← `financial.addon_purchases` | `owner_managed` | `cut_over` | billing and finance operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_api_me_addons.py`<br>`tests/test_billing_addon_contract_backfill.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `financial.payment_arrangement_staff_actions` | atomic staff arrangement transition and audit coordination | `application_coordinator` | canonical payment-arrangement action preview ← `financial.payment_arrangements`<br>authorized staff command context ← `auth.permission_gate` | `coordinator_managed` | `complete` | billing operations | `docs/designs/PAYMENT_ARRANGEMENT_SAFE_ACTIONS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_payment_arrangement_safe_actions.py`<br>`tests/test_payment_arrangements.py`<br>`tests/architecture/test_action_form_ownership.py` |
| `financial.access_resolution` | billable service classification | `policy` | canonical subscriber account state ← `customer.accounts`<br>canonical subscription lifecycle state ← `access.subscription_lifecycle`<br>canonical billing profile ← `financial.billing_profile` | `read_only` | `complete` | billing and network access | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_access_resolution.py`<br>`tests/test_customer_service_state.py`<br>`tests/test_prepaid_threshold_resolver.py`<br>`tests/architecture/test_access_resolution_boundary.py` |
| `financial.access_resolution` | RADIUS access decision | `policy` | canonical subscriber account state ← `customer.accounts`<br>canonical subscription lifecycle state ← `access.subscription_lifecycle`<br>canonical access restriction intent ← `access.walled_garden_policy` | `read_only` | `complete` | billing and network access | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_access_resolution.py`<br>`tests/test_customer_service_state.py`<br>`tests/test_prepaid_threshold_resolver.py`<br>`tests/architecture/test_access_resolution_boundary.py` |
| `financial.access_resolution` | financial suspension/restoration eligibility | `policy` | canonical subscriber account state ← `customer.accounts`<br>canonical subscription lifecycle state ← `access.subscription_lifecycle`<br>canonical billing profile ← `financial.billing_profile`<br>currency-bound customer financial position ← `customer.financial_position`<br>canonical prepaid threshold ← `financial.prepaid_threshold` | `read_only` | `complete` | billing and network access | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_access_resolution.py`<br>`tests/test_customer_service_state.py`<br>`tests/test_prepaid_threshold_resolver.py`<br>`tests/architecture/test_access_resolution_boundary.py` |
| `financial.access_resolution` | currency-bound prepaid funding decision | `policy` | currency-bound customer financial position ← `customer.financial_position`<br>canonical prepaid threshold ← `financial.prepaid_threshold`<br>prepaid enforcement currency setting ← `financial.prepaid_currency` | `read_only` | `complete` | billing and network access | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_access_resolution.py`<br>`tests/test_customer_service_state.py`<br>`tests/test_prepaid_threshold_resolver.py`<br>`tests/architecture/test_access_resolution_boundary.py` |
| `financial.dunning_staff_actions` | atomic staff dunning-case transition and audit coordination | `application_coordinator` | canonical dunning staff-action impact ← `financial.dunning`<br>authorized dunning staff command context ← `auth.permission_gate` | `coordinator_managed` | `complete` | collections operations | `docs/designs/DUNNING_STAFF_SAFE_ACTIONS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_dunning_staff_safe_actions.py`<br>`tests/test_web_billing_dunning.py`<br>`tests/architecture/test_action_form_ownership.py` |
| `financial.billing_automation` | postpaid recurring charge preview | `resolver` | canonical billable subscription facts ← `access.subscription_lifecycle` | `owner_managed` | `complete` | billing operations | `docs/designs/INVOICE_BATCH_AND_REMINDER_SAFE_ACTIONS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_invoice_batch_web.py`<br>`tests/test_billing_automation_services.py`<br>`tests/architecture/test_action_form_ownership.py` |
| `financial.billing_automation` | postpaid invoice batch execution | `command_writer` | canonical billable subscription facts ← `access.subscription_lifecycle`<br>confirmed staff batch evidence ← `ui.invoice_batch_action_projection` | `owner_managed` | `complete` | billing operations | `docs/designs/INVOICE_BATCH_AND_REMINDER_SAFE_ACTIONS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_invoice_batch_web.py`<br>`tests/test_billing_automation_services.py`<br>`tests/architecture/test_action_form_ownership.py` |
| `financial.billing_automation` | durable billing-run lifecycle and retry lineage | `authoritative_record` | confirmed staff batch evidence ← `ui.invoice_batch_action_projection` | `owner_managed` | `complete` | billing operations | `docs/designs/INVOICE_BATCH_AND_REMINDER_SAFE_ACTIONS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_invoice_batch_web.py`<br>`tests/test_billing_automation_services.py`<br>`tests/architecture/test_action_form_ownership.py` |
| `financial.billing_automation` | billing-run audit projection and repair | `projection_writer` | canonical billing-run record ← `financial.billing_automation` | `owner_managed` | `complete` | billing operations | `docs/designs/INVOICE_BATCH_AND_REMINDER_SAFE_ACTIONS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_invoice_batch_web.py`<br>`tests/test_billing_automation_services.py`<br>`tests/architecture/test_action_form_ownership.py` |
| `financial.payment_provider_events` | payment-provider event ingestion | `observation_collector` | verified external provider observation ← `external:payment_provider`<br>administrative provider observation ← `financial.payment_provider_events`<br>active provider identity ← `financial.payment_routing`<br>provider-event command context ← `financial.payment_provider_events` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_provider_events.py`<br>`tests/test_api_billing_webhooks.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/architecture/test_payment_provider_event_ownership.py`<br>`tests/architecture/test_payment_settlement_participants.py`<br>`tests/integration/test_payment_provider_event_concurrency.py` |
| `financial.payment_provider_events` | normalized provider monetary observations | `authoritative_record` | verified external provider observation ← `external:payment_provider`<br>active provider identity ← `financial.payment_routing` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_provider_events.py`<br>`tests/test_api_billing_webhooks.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/architecture/test_payment_provider_event_ownership.py`<br>`tests/architecture/test_payment_settlement_participants.py`<br>`tests/integration/test_payment_provider_event_concurrency.py` |
| `financial.payment_provider_events` | provider-event idempotency | `authoritative_record` | canonical provider-event record ← `financial.payment_provider_events`<br>provider-event command context ← `financial.payment_provider_events` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_provider_events.py`<br>`tests/test_api_billing_webhooks.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/architecture/test_payment_provider_event_ownership.py`<br>`tests/architecture/test_payment_settlement_participants.py`<br>`tests/integration/test_payment_provider_event_concurrency.py` |
| `financial.payment_provider_events` | incomplete provider settlement resumption | `command_writer` | canonical provider-event record ← `financial.payment_provider_events`<br>canonical payment participant protocol ← `financial.payments`<br>canonical consolidated-payment participant protocol ← `financial.consolidated_payments`<br>canonical invoice-settlement participant protocol ← `financial.provider_payment_settlements`<br>provider-event command context ← `financial.payment_provider_events` | `owner_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_payment_provider_events.py`<br>`tests/test_api_billing_webhooks.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/architecture/test_payment_provider_event_ownership.py`<br>`tests/architecture/test_payment_settlement_participants.py`<br>`tests/integration/test_payment_provider_event_concurrency.py` |
| `financial.payment_webhooks` | verified payment webhook projection | `application_coordinator` | claimed signature-verified payment receipt ← `integration.inbox`<br>external provider payment observation ← `external:payment_provider`<br>canonical provider-event settlement protocol ← `financial.payment_provider_events` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_api_billing_webhooks.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/architecture/test_payment_webhook_ownership.py`<br>`tests/architecture/test_payment_settlement_participants.py` |
| `financial.payment_webhooks` | billing consequence submission from verified receipts | `application_coordinator` | claimed signature-verified payment receipt ← `integration.inbox`<br>canonical provider-event settlement protocol ← `financial.payment_provider_events`<br>canonical account-credit deposit protocol ← `financial.account_credit_deposits`<br>canonical top-up completion protocol ← `financial.topup_intents`<br>canonical inbox consequence protocol ← `integration.inbox` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_api_billing_webhooks.py`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/architecture/test_payment_webhook_ownership.py`<br>`tests/architecture/test_payment_settlement_participants.py` |
| `financial.payment_reconciliation` | stranded top-up reconciliation | `application_coordinator` | canonical top-up reconciliation policy ← `control.settings_spec`<br>canonical pending top-up intent ← `financial.topup_intents`<br>external gateway verification observation ← `external:payment_provider`<br>canonical top-up expiry protocol ← `financial.topup_intents` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`docs/runbooks/PAYSTACK_AUTOMATIC_POSTING.md`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_reconcile_honours_invoice_intent.py`<br>`tests/architecture/test_payment_reconciliation_ownership.py`<br>`tests/architecture/test_payment_settlement_participants.py` |
| `financial.payment_reconciliation` | scheduled top-up reconciliation execution | `application_coordinator` | canonical top-up reconciliation policy ← `control.settings_spec`<br>canonical pending top-up intent ← `financial.topup_intents`<br>external gateway verification observation ← `external:payment_provider` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`docs/runbooks/PAYSTACK_AUTOMATIC_POSTING.md`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_reconcile_honours_invoice_intent.py`<br>`tests/architecture/test_payment_reconciliation_ownership.py`<br>`tests/architecture/test_payment_settlement_participants.py` |
| `financial.payment_reconciliation` | verified provider settlement then allocation orchestration | `application_coordinator` | canonical pending top-up intent ← `financial.topup_intents`<br>external gateway verification observation ← `external:payment_provider`<br>canonical account-credit deposit protocol ← `financial.account_credit_deposits`<br>canonical provider-event settlement protocol ← `financial.payment_provider_events`<br>canonical top-up completion protocol ← `financial.topup_intents` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`docs/runbooks/PAYSTACK_AUTOMATIC_POSTING.md`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_reconcile_honours_invoice_intent.py`<br>`tests/architecture/test_payment_reconciliation_ownership.py`<br>`tests/architecture/test_payment_settlement_participants.py` |
| `financial.payment_reconciliation` | top-up reconciliation backlog projection | `resolver` | canonical top-up reconciliation policy ← `control.settings_spec`<br>canonical pending top-up intent ← `financial.topup_intents` | `coordinator_managed` | `complete` | finance operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`docs/runbooks/PAYSTACK_AUTOMATIC_POSTING.md`<br>`tests/test_payment_webhook_settlement.py`<br>`tests/test_reconcile_honours_invoice_intent.py`<br>`tests/architecture/test_payment_reconciliation_ownership.py`<br>`tests/architecture/test_payment_settlement_participants.py` |
| `network.as_built_plant_projection` | fiber segment projection of accepted vendor as-built evidence | `reconciler` | accepted vendor as-built evidence ← `operations.vendor_project_records` | `participant` | `native` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_as_built_plant_projection.py` |
| `network.radius_sessions` | online-now session state | `resolver` | canonical live RADIUS observations ← `sessions.radius_reconciliation`<br>canonical subscription cohort ← `access.subscription_lifecycle` | `read_only` | `complete` | network operations | `docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_network_sot_services.py`<br>`tests/test_portal_account_health.py` |
| `network.radius_sessions` | active-session NAS observation evidence | `resolver` | canonical live RADIUS observations ← `sessions.radius_reconciliation`<br>canonical network identities ← `network.identity` | `read_only` | `complete` | network operations | `docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_network_sot_services.py`<br>`tests/test_portal_account_health.py` |
| `network.radius_sessions` | bounded historical NAS evidence | `resolver` | canonical RADIUS history ← `sessions.radius_reconciliation` | `read_only` | `complete` | network operations | `docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_network_sot_services.py`<br>`tests/test_portal_account_health.py` |
| `network.radius_sessions` | subscription-scoped live-session binding and freshness projection | `resolver` | canonical live RADIUS observations ← `sessions.radius_reconciliation`<br>canonical subscription cohort ← `access.subscription_lifecycle`<br>session freshness policy ← `network.radius_sessions` | `read_only` | `complete` | network operations | `docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_network_sot_services.py`<br>`tests/test_portal_account_health.py` |
| `network.device_state` | binary NOC-facing device operational outcome | `resolver` | device administrative lifecycle ← `network.monitoring_inventory`<br>native reachability observations ← `runtime.infrastructure_polling`<br>ONT runtime observations ← `network.ont_runtime_status`<br>monitoring path observations ← `external:wireguard` | `read_only` | `complete` | network operations | `docs/designs/DEVICE_OPERATIONAL_STATUS.md`<br>`docs/designs/SCHEDULER_CONTROL_LIFECYCLE.md`<br>`tests/test_device_operational_status.py`<br>`tests/test_operational_status_per_type.py`<br>`tests/architecture/test_binary_device_operational_lifecycle.py` |
| `network.device_state` | device operational status vocabulary and reason classification | `resolver` | device administrative lifecycle ← `network.monitoring_inventory`<br>native reachability observations ← `runtime.infrastructure_polling`<br>ONT runtime observations ← `network.ont_runtime_status`<br>monitoring path observations ← `external:wireguard` | `read_only` | `complete` | network operations | `docs/designs/DEVICE_OPERATIONAL_STATUS.md`<br>`docs/designs/SCHEDULER_CONTROL_LIFECYCLE.md`<br>`tests/test_device_operational_status.py`<br>`tests/test_operational_status_per_type.py`<br>`tests/architecture/test_binary_device_operational_lifecycle.py` |
| `network.device_state` | device verification-due, impairment, and alarm classification | `policy` | device administrative lifecycle ← `network.monitoring_inventory`<br>native reachability observations ← `runtime.infrastructure_polling`<br>ONT runtime observations ← `network.ont_runtime_status`<br>monitoring path observations ← `external:wireguard` | `read_only` | `complete` | network operations | `docs/designs/DEVICE_OPERATIONAL_STATUS.md`<br>`docs/designs/SCHEDULER_CONTROL_LIFECYCLE.md`<br>`tests/test_device_operational_status.py`<br>`tests/test_operational_status_per_type.py`<br>`tests/architecture/test_binary_device_operational_lifecycle.py` |
| `network.device_projection` | device_projections materialised table | `projection_writer` | canonical device identity ← `network.identity`<br>monitoring inventory observations ← `network.monitoring_inventory`<br>resolved operational device state ← `network.device_state` | `owner_managed` | `native` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SCHEDULER_CONTROL_LIFECYCLE.md`<br>`tests/test_owner_commands.py`<br>`tests/test_device_projection_reconcile.py`<br>`tests/test_device_projection_task.py`<br>`tests/architecture/test_owner_command_boundary.py`<br>`tests/architecture/test_scheduler_boolean_control_boundary.py` |
| `network.device_projection` | unified cross-type device row (OLT/core/ONT/CPE) | `projection_writer` | canonical device identity ← `network.identity`<br>monitoring inventory observations ← `network.monitoring_inventory` | `owner_managed` | `native` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SCHEDULER_CONTROL_LIFECYCLE.md`<br>`tests/test_owner_commands.py`<br>`tests/test_device_projection_reconcile.py`<br>`tests/test_device_projection_task.py`<br>`tests/architecture/test_owner_command_boundary.py`<br>`tests/architecture/test_scheduler_boolean_control_boundary.py` |
| `network.device_projection` | projected binary operational status and repair evidence | `projection_writer` | resolved operational device state ← `network.device_state`<br>monitoring inventory observations ← `network.monitoring_inventory` | `owner_managed` | `native` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SCHEDULER_CONTROL_LIFECYCLE.md`<br>`tests/test_owner_commands.py`<br>`tests/test_device_projection_reconcile.py`<br>`tests/test_device_projection_task.py`<br>`tests/architecture/test_owner_command_boundary.py`<br>`tests/architecture/test_scheduler_boolean_control_boundary.py` |
| `network.device_projection` | device projection orphan pruning | `reconciler` | canonical device identity ← `network.identity` | `owner_managed` | `native` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SCHEDULER_CONTROL_LIFECYCLE.md`<br>`tests/test_owner_commands.py`<br>`tests/test_device_projection_reconcile.py`<br>`tests/test_device_projection_task.py`<br>`tests/architecture/test_owner_command_boundary.py`<br>`tests/architecture/test_scheduler_boolean_control_boundary.py` |
| `network.tr069_commands` | TR-069 command admission coordination | `application_coordinator` | authenticated TR-069 command evidence ← `auth.permission_gate`<br>canonical TR-069 device and ACS binding ← `network.identity`<br>TR-069 command admission capability ← `control.feature_registry`<br>canonical network operation lifecycle ← `network.operation_ledger`<br>durable network command dispatch ← `network.operation_dispatch` | `coordinator_managed` | `complete` | network operations | `docs/designs/TR069_COMMAND_LIFECYCLE.md`<br>`docs/runbooks/TR069_COMMAND_CUTOVER.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`tests/test_tr069_job_commands.py`<br>`tests/architecture/test_tr069_job_lifecycle_boundary.py` |
| `network.tr069_commands` | TR-069 command execution coordination | `application_coordinator` | canonical TR-069 device and ACS binding ← `network.identity`<br>canonical network operation lifecycle ← `network.operation_ledger`<br>durable network command dispatch ← `network.operation_dispatch` | `coordinator_managed` | `complete` | network operations | `docs/designs/TR069_COMMAND_LIFECYCLE.md`<br>`docs/runbooks/TR069_COMMAND_CUTOVER.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`tests/test_tr069_job_commands.py`<br>`tests/architecture/test_tr069_job_lifecycle_boundary.py` |
| `network.tr069_commands` | TR-069 command outcome coordination | `application_coordinator` | canonical network operation lifecycle ← `network.operation_ledger`<br>durable network command dispatch ← `network.operation_dispatch`<br>normalized GenieACS command observation ← `external:genieacs` | `coordinator_managed` | `complete` | network operations | `docs/designs/TR069_COMMAND_LIFECYCLE.md`<br>`docs/runbooks/TR069_COMMAND_CUTOVER.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`tests/test_tr069_job_commands.py`<br>`tests/architecture/test_tr069_job_lifecycle_boundary.py` |
| `network.cpe_dialer_credential` | derived CPE PPPoE dialer credential projection | `reconciler` | authoritative subscriber access credential ← `access.radius_projection`<br>active ONT-to-subscriber assignment ← `network.identity`<br>derived CPE dialer projection ← `network.cpe_dialer_credential`<br>credential fingerprint key ← `secrets.credential_crypto` | `participant` | `shadowing` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_cpe_dialer_credential_reconcile.py` |
| `network.cpe_dialer_credential` | CPE dialer credential fingerprint comparison and readback | `resolver` | authoritative subscriber access credential ← `access.radius_projection`<br>derived CPE dialer projection ← `network.cpe_dialer_credential`<br>ACS-reported PPPoE dialer username ← `external:genieacs`<br>credential fingerprint key ← `secrets.credential_crypto` | `participant` | `shadowing` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_cpe_dialer_credential_reconcile.py` |
| `network.ip_assignment_lifecycle` | exact service ownership of active IPv4 assignments | `reconciler` | canonical active IPv4 assignment ← `network.ip_assignment_lifecycle`<br>canonical active subscription identity ← `access.subscription_lifecycle`<br>served IPv4 compatibility projection ← `network.ip_assignment_lifecycle`<br>reviewed ownership repair command ← `network.ip_assignment_lifecycle` | `owner_managed` | `shadowing` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md`<br>`tests/test_ip_assignment_repair.py`<br>`tests/test_ip_assignment_lifecycle.py`<br>`tests/architecture/test_ip_assignment_service_ownership.py` |
| `network.ip_assignment_lifecycle` | reviewed exact-service IPv4 assignment lifecycle repair | `command_writer` | canonical active IPv4 assignment ← `network.ip_assignment_lifecycle`<br>canonical active subscription identity ← `access.subscription_lifecycle`<br>serviceable IPv4 address inventory ← `network.ip_assignment_lifecycle`<br>reviewed lifecycle repair command ← `network.ip_assignment_lifecycle` | `owner_managed` | `shadowing` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md`<br>`tests/test_ip_assignment_repair.py`<br>`tests/test_ip_assignment_lifecycle.py`<br>`tests/architecture/test_ip_assignment_service_ownership.py` |
| `network.ip_assignment_lifecycle` | reviewed exact-service IPv4 served projection repair | `command_writer` | canonical active IPv4 assignment ← `network.ip_assignment_lifecycle`<br>canonical active subscription identity ← `access.subscription_lifecycle`<br>served IPv4 compatibility projection ← `network.ip_assignment_lifecycle`<br>observed RADIUS IPv4 projection ← `access.radius_projection`<br>active RADIUS session observation ← `sessions.radius_reconciliation`<br>reviewed served projection repair command ← `network.ip_assignment_lifecycle` | `owner_managed` | `shadowing` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md`<br>`tests/test_ip_assignment_repair.py`<br>`tests/test_ip_assignment_lifecycle.py`<br>`tests/architecture/test_ip_assignment_service_ownership.py` |
| `network.outage_lifecycle` | persisted outage incident status vocabulary | `event_policy` | recorded outage incidents ← `network.outage_lifecycle` | `owner_managed` | `complete` | network operations | `docs/designs/NETWORK_OUTAGE_RESPONSE_LIFECYCLE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/services/topology/test_outage_lifecycle_chain.py`<br>`tests/architecture/test_outage_lifecycle_chain_boundary.py`<br>`tests/services/topology/test_outage_reconcile.py` |
| `network.outage_lifecycle` | outage incident lifecycle | `authoritative_record` | recorded outage incidents ← `network.outage_lifecycle`<br>resolved outage impact ← `network.outage_impact` | `owner_managed` | `complete` | network operations | `docs/designs/NETWORK_OUTAGE_RESPONSE_LIFECYCLE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/services/topology/test_outage_lifecycle_chain.py`<br>`tests/architecture/test_outage_lifecycle_chain_boundary.py`<br>`tests/services/topology/test_outage_reconcile.py` |
| `network.outage_lifecycle` | typed outage lifecycle output emission | `command_writer` | recorded outage incidents ← `network.outage_lifecycle` | `owner_managed` | `complete` | network operations | `docs/designs/NETWORK_OUTAGE_RESPONSE_LIFECYCLE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/services/topology/test_outage_lifecycle_chain.py`<br>`tests/architecture/test_outage_lifecycle_chain_boundary.py`<br>`tests/services/topology/test_outage_reconcile.py` |
| `network.outage_lifecycle` | committed outage output consumption | `command_writer` | recorded outage incidents ← `network.outage_lifecycle`<br>operational escalation surface ← `operations.sla_escalation`<br>receipted owner-output deliveries ← `events.owner_outputs` | `owner_managed` | `complete` | network operations | `docs/designs/NETWORK_OUTAGE_RESPONSE_LIFECYCLE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/services/topology/test_outage_lifecycle_chain.py`<br>`tests/architecture/test_outage_lifecycle_chain_boundary.py`<br>`tests/services/topology/test_outage_reconcile.py` |
| `network.outage_auto_notify` | automation eligibility for customer outage notification | `policy` | classifier outage incident ← `network.outage_lifecycle`<br>automation gate configuration ← `control.settings_spec` | `owner_managed` | `native` | Network operations | `docs/adr/0004-automated-outage-notification-dispatch.md`<br>`docs/designs/OUTAGE_CLASSIFIER.md`<br>`tests/test_outage_auto_notify.py` |
| `network.outage_auto_notify` | automated dispatch trigger and its transaction | `application_coordinator` | classifier outage incident ← `network.outage_lifecycle`<br>affected subscription set ← `network.outage_impact`<br>automation gate configuration ← `control.settings_spec` | `owner_managed` | `native` | Network operations | `docs/adr/0004-automated-outage-notification-dispatch.md`<br>`docs/designs/OUTAGE_CLASSIFIER.md`<br>`tests/test_outage_auto_notify.py` |
| `sessions.radius_resolution` | customer online-now resolution | `resolver` | active RADIUS session projection ← `sessions.radius_reconciliation` | `read_only` | `native` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/DASHBOARD_OVERVIEW_PAGE_CONTRACT.md`<br>`tests/test_network_sot_services.py`<br>`tests/test_sot_relationships.py` |
| `sessions.radius_resolution` | primary NAS session resolution | `resolver` | active RADIUS session projection ← `sessions.radius_reconciliation`<br>network identity registry ← `network.identity` | `read_only` | `native` | network operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/DASHBOARD_OVERVIEW_PAGE_CONTRACT.md`<br>`tests/test_network_sot_services.py`<br>`tests/test_sot_relationships.py` |
| `communications.customer_policy` | customer notification eligibility | `policy` | customer notification identity and preferences ← `customer.accounts`<br>account notification status ← `customer.accounts`<br>channel configuration ← `communications.channel_policy`<br>recipient suppression ledger ← `communications.eligibility`<br>recent notification history ← `communications.notification_service`<br>evaluation time ← `external:system_clock` | `read_only` | `native` | customer communications | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_customer_bulk_actions.py`<br>`tests/test_communication_eligibility.py`<br>`tests/architecture/test_customer_notification_policy_boundary.py` |
| `communications.customer_policy` | cohort-batched customer notification eligibility | `policy` | customer notification identity and preferences ← `customer.accounts`<br>account notification status ← `customer.accounts`<br>channel configuration ← `communications.channel_policy`<br>recipient suppression ledger ← `communications.eligibility`<br>recent notification history ← `communications.notification_service`<br>evaluation time ← `external:system_clock` | `read_only` | `native` | customer communications | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/CODING_STANDARD.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_customer_bulk_actions.py`<br>`tests/test_communication_eligibility.py`<br>`tests/architecture/test_customer_notification_policy_boundary.py` |
| `operations.sla_escalation` | operational SLA event policy lifecycle | `authoritative_record` | validated SLA policy command ← `operations.sla_escalation_commands`<br>current operational SLA records ← `operations.sla_escalation` | `participant` | `complete` | operations platform | `docs/ARCHITECTURE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_operational_escalation.py`<br>`tests/test_operational_sla_policy_ui.py`<br>`tests/architecture/test_operational_sla_policy_ownership.py` |
| `operations.sla_escalation` | event-scoped escalation timing and channel policy | `event_policy` | current operational SLA records ← `operations.sla_escalation`<br>validated operational event observation ← `operations.sla_escalation` | `participant` | `complete` | operations platform | `docs/ARCHITECTURE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_operational_escalation.py`<br>`tests/test_operational_sla_policy_ui.py`<br>`tests/architecture/test_operational_sla_policy_ownership.py` |
| `operations.sla_escalation` | operational escalation event and delivery planning | `authoritative_record` | current operational SLA records ← `operations.sla_escalation`<br>validated operational event observation ← `operations.sla_escalation`<br>operational participant records ← `operations.sla_escalation` | `participant` | `complete` | operations platform | `docs/ARCHITECTURE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_operational_escalation.py`<br>`tests/test_operational_sla_policy_ui.py`<br>`tests/architecture/test_operational_sla_policy_ownership.py` |
| `operations.sla_escalation` | operational escalation acknowledgement and cancellation | `command_writer` | authenticated escalation command evidence ← `auth.permission_gate`<br>current operational SLA records ← `operations.sla_escalation` | `participant` | `complete` | operations platform | `docs/ARCHITECTURE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_operational_escalation.py`<br>`tests/test_operational_sla_policy_ui.py`<br>`tests/architecture/test_operational_sla_policy_ownership.py` |
| `operations.sla_escalation_commands` | operational SLA policy command confirmation | `application_coordinator` | authenticated SLA policy command evidence ← `auth.permission_gate`<br>current operational SLA records ← `operations.sla_escalation` | `coordinator_managed` | `complete` | operations platform | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_operational_sla_policy_ui.py`<br>`tests/architecture/test_operational_sla_policy_ownership.py`<br>`tests/architecture/test_owner_command_boundary.py` |
| `communications.team_inbox_participants` | conversation participant endpoint projection | `projection_writer` | stored conversation message headers ← `communications.team_inbox_threads`<br>owned mailbox register ← `communications.team_inbox_routing` | `participant` | `complete` | customer experience platform | `docs/designs/INBOX_CONVERSATION_PARTICIPANTS.md`<br>`tests/test_team_inbox_participants.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_observations` | normalized inbound provider observation ledger | `observation_collector` | verified normalized provider fact ← `external:communications_provider`<br>verified webhook admission ← `integration.inbox` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_field_job` | field job chat conversation lifecycle | `policy` | committed field job departure ← `operations.field_completion` | `participant` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_field_job` | work order to inbox conversation link | `authoritative_record` | committed field job departure ← `operations.field_completion` | `participant` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_processing` | provider observation consequence coordination | `application_coordinator` | committed normalized observation ← `communications.team_inbox_observations`<br>conversation identity ← `communications.team_inbox_threads`<br>contact decision ← `communications.team_inbox_contact_resolution`<br>routing decision ← `communications.team_inbox_routing`<br>delivery receipt state ← `communications.team_inbox_delivery_receipts` | `coordinator_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_threads` | conversation identity and threading | `resolver` | normalized inbound message fact ← `communications.team_inbox_observations` | `participant` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_threads` | authoritative conversation and message records | `authoritative_record` | normalized inbound message fact ← `communications.team_inbox_observations` | `participant` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_contact_resolution` | contact subscriber reseller and ticket association resolution | `resolver` | canonical party contact facts ← `party.registry`<br>customer identity scope ← `customer.identity_scope`<br>conversation contact route ← `communications.team_inbox_threads` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_contact_resolution` | reviewed contact association and projection repair | `projection_writer` | canonical party contact facts ← `party.registry`<br>customer identity scope ← `customer.identity_scope`<br>conversation contact route ← `communications.team_inbox_threads` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_routing` | routing assignment and escalation policy | `policy` | conversation routing facts ← `communications.team_inbox_threads`<br>operational escalation policy ← `operations.sla_escalation`<br>operator authorization ← `auth.permission_gate` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_routing` | routing assignment and escalation transitions | `command_writer` | conversation routing facts ← `communications.team_inbox_threads`<br>operational escalation policy ← `operations.sla_escalation`<br>operator authorization ← `auth.permission_gate` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_operator_state` | operator read cursor | `command_writer` | message chronology ← `communications.team_inbox_threads`<br>operator principal ← `auth.permission_gate` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_operator_state` | operator unread projection repair | `reconciler` | message chronology ← `communications.team_inbox_threads`<br>operator principal ← `auth.permission_gate` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_outbound_intents` | transactional outbound communication intent | `command_writer` | conversation reply target ← `communications.team_inbox_threads`<br>communication intent lifecycle ← `communications.intents`<br>effective channel policy ← `communications.channel_policy` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_outbound_intents` | outbound Inbox message attempt projection | `projection_writer` | conversation reply target ← `communications.team_inbox_threads`<br>communication intent lifecycle ← `communications.intents`<br>effective channel policy ← `communications.channel_policy` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_delivery_receipts` | provider delivery receipt reconciliation | `projection_writer` | normalized receipt ← `communications.team_inbox_observations`<br>outbound attempt identity ← `communications.team_inbox_outbound_intents` | `participant` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_commands` | operator conversation and collaboration commands | `application_coordinator` | authenticated operator command ← `auth.permission_gate`<br>current conversation state ← `communications.team_inbox_threads`<br>contact association decision ← `communications.team_inbox_contact_resolution`<br>routing transition decision ← `communications.team_inbox_routing`<br>outbound intent outcome ← `communications.team_inbox_outbound_intents`<br>operator read state ← `communications.team_inbox_operator_state` | `coordinator_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_widget` | authenticated visitor message and read-state commands | `command_writer` | authenticated visitor principal ← `customer.identity_scope`<br>widget conversation identity ← `communications.team_inbox_threads`<br>live-chat authority selection ← `control.settings_spec` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/adr/0006-temporary-crm-chat-authority.md`<br>`docs/runbooks/TEMPORARY_CRM_CHAT_AUTHORITY.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_chat_session.py`<br>`tests/test_team_inbox_widget_native.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_projection` | Inbox list detail metrics response cohort unread and action projection | `resolver` | conversation records ← `communications.team_inbox_threads`<br>contact projection ← `communications.team_inbox_contact_resolution`<br>routing state ← `communications.team_inbox_routing`<br>delivery projection ← `communications.team_inbox_delivery_receipts`<br>unread projection ← `communications.team_inbox_operator_state`<br>ticket handoff provenance ← `communications.conversation_ticket_handoff` | `read_only` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/test_team_inbox_needs_attention.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_maintenance` | scheduled Inbox projection maintenance and repair | `reconciler` | current Inbox projection ← `communications.team_inbox_projection`<br>canonical conversation identity ← `communications.team_inbox_threads`<br>outbound intent state ← `communications.team_inbox_outbound_intents` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_realtime` | best-effort realtime Inbox projection and rebuild | `transport` | current Inbox projection ← `communications.team_inbox_projection` | `not_applicable` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_smtp_transport` | dedicated SMTP intake process and envelope transport | `transport` | SMTP envelope and RFC822 bytes ← `external:customer_mail_server` | `not_applicable` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_health` | verified SMTP probe delivery projection | `projection_writer` | exact synthetic SMTP message ← `communications.team_inbox_threads` | `owner_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.team_inbox_campaigns` | campaign-sourced conversation and message materialization | `projection_writer` | canonical conversation identity ← `communications.team_inbox_threads`<br>outbound intent ← `communications.team_inbox_outbound_intents` | `participant` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_SOURCE_OF_TRUTH.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_team_inbox_sot_completion.py`<br>`tests/architecture/test_team_inbox_boundaries.py`<br>`tests/architecture/test_team_inbox_sot_contracts.py` |
| `communications.conversation_ticket_handoff` | conversation-to-ticket issuance eligibility | `application_coordinator` | canonical conversation state ← `communications.team_inbox_threads`<br>typed issuance request ← `communications.conversation_ticket_handoff`<br>ticket command result ← `support.ticket_lifecycle` | `coordinator_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_conversation_ticket_handoff.py`<br>`tests/architecture/test_conversation_ticket_handoff_boundary.py` |
| `communications.conversation_ticket_handoff` | native conversation-to-ticket provenance | `command_writer` | canonical conversation state ← `communications.team_inbox_threads`<br>typed issuance request ← `communications.conversation_ticket_handoff`<br>ticket command result ← `support.ticket_lifecycle` | `coordinator_managed` | `complete` | customer experience platform | `docs/designs/TEAM_INBOX_ADMIN_UI_PORT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_conversation_ticket_handoff.py`<br>`tests/architecture/test_conversation_ticket_handoff_boundary.py` |
| `events.owner_outputs` | versioned owner-output envelope | `policy` | producing owner command evidence ← `events.dispatcher`<br>staged outbox events ← `events.store` | `participant` | `shadowing` | platform and billing operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_owner_outputs.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `events.owner_outputs` | durable owner-output consumer receipts | `authoritative_record` | producing owner command evidence ← `events.dispatcher`<br>recorded consumer receipts ← `events.owner_outputs` | `participant` | `shadowing` | platform and billing operations | `docs/adr/0007-end-to-end-billing-target-architecture.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_owner_outputs.py`<br>`tests/architecture/test_billing_target_architecture.py` |
| `runtime.realtime_projection` | versioned real-time event envelope | `policy` | real-time schema contract ← `runtime.realtime_projection` | `not_applicable` | `complete` | platform runtime | `docs/REALTIME_PLATFORM.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_realtime_platform.py`<br>`tests/test_realtime_subscriptions.py`<br>`tests/architecture/test_realtime_platform_boundary.py` |
| `runtime.realtime_projection` | Redis topic naming and best-effort publication | `transport` | real-time schema contract ← `runtime.realtime_projection`<br>committed projection request ← `runtime.realtime_projection`<br>Redis delivery availability observation ← `external:redis` | `not_applicable` | `complete` | platform runtime | `docs/REALTIME_PLATFORM.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_realtime_platform.py`<br>`tests/test_realtime_subscriptions.py`<br>`tests/architecture/test_realtime_platform_boundary.py` |
| `runtime.realtime_projection` | shared WebSocket and SSE delivery semantics | `transport` | real-time schema contract ← `runtime.realtime_projection`<br>authorized subscription topics ← `auth.permission_gate`<br>Redis delivery availability observation ← `external:redis` | `not_applicable` | `complete` | platform runtime | `docs/REALTIME_PLATFORM.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_realtime_platform.py`<br>`tests/test_realtime_subscriptions.py`<br>`tests/architecture/test_realtime_platform_boundary.py` |
| `runtime.realtime_projection` | reconnect and no-replay refresh contract | `policy` | real-time schema contract ← `runtime.realtime_projection`<br>Redis delivery availability observation ← `external:redis` | `not_applicable` | `complete` | platform runtime | `docs/REALTIME_PLATFORM.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_realtime_platform.py`<br>`tests/test_realtime_subscriptions.py`<br>`tests/architecture/test_realtime_platform_boundary.py` |
| `observability.database_diagnostics` | redacted database schema-error correlation | `resolver` | database driver failure observation ← `runtime.db_sessions`<br>request correlation context ← `observability.recording` | `not_applicable` | `native` | platform operations | `docs/designs/OPERATIONAL_EVIDENCE_AND_RETRY.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_operational_evidence_followup.py` |
| `observability.database_diagnostics` | redacted idle-transaction failure correlation | `resolver` | database driver failure observation ← `runtime.db_sessions`<br>request correlation context ← `observability.recording` | `not_applicable` | `native` | platform operations | `docs/designs/OPERATIONAL_EVIDENCE_AND_RETRY.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_operational_evidence_followup.py` |
| `operations.service_team_source_retirement` | legacy service-team source retirement | `command_writer` | native service-team identity pointers ← `operations.service_team_lifecycle`<br>legacy workflow service-team sources ← `operations.service_team_source_retirement` | `owner_managed` | `cutover_ready` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/runbooks/SERVICE_TEAM_PARTY_CUTOVER.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_service_team_source_retirement.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.service_team_source_retirement` | legacy service-team source-retirement readiness | `resolver` | native service-team identity pointers ← `operations.service_team_lifecycle`<br>legacy workflow service-team sources ← `operations.service_team_source_retirement` | `owner_managed` | `cutover_ready` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/runbooks/SERVICE_TEAM_PARTY_CUTOVER.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_service_team_source_retirement.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.service_team_lifecycle` | service-team lifecycle | `authoritative_record` | typed service-team command ← `operations.service_team_lifecycle`<br>active staff authentication principal ← `auth.staff_provisioning`<br>canonical Person Party identity ← `party.registry`<br>current native service-team state ← `operations.service_team_lifecycle` | `owner_managed` | `cutover_ready` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_service_team_lifecycle.py`<br>`tests/test_service_team_web.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.service_team_lifecycle` | service-team membership lifecycle | `authoritative_record` | typed service-team command ← `operations.service_team_lifecycle`<br>active staff authentication principal ← `auth.staff_provisioning`<br>canonical Person Party identity ← `party.registry`<br>current native service-team state ← `operations.service_team_lifecycle` | `owner_managed` | `cutover_ready` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_service_team_lifecycle.py`<br>`tests/test_service_team_web.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.service_team_lifecycle` | set-valued staff service-team membership resolution | `resolver` | active staff authentication principal ← `auth.staff_provisioning`<br>canonical Person Party identity ← `party.registry`<br>current native service-team state ← `operations.service_team_lifecycle` | `owner_managed` | `cutover_ready` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_service_team_lifecycle.py`<br>`tests/test_service_team_web.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.service_team_lifecycle` | active service-team selector projection | `resolver` | current native service-team state ← `operations.service_team_lifecycle` | `owner_managed` | `cutover_ready` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_service_team_lifecycle.py`<br>`tests/test_service_team_web.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.service_team_lifecycle` | service-team administration projection | `resolver` | active staff authentication principal ← `auth.staff_provisioning`<br>canonical Person Party identity ← `party.registry`<br>current native service-team state ← `operations.service_team_lifecycle`<br>current service-team composition ← `operations.service_team_composition` | `owner_managed` | `cutover_ready` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_service_team_lifecycle.py`<br>`tests/test_service_team_web.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.service_team_composition` | service-team composition lifecycle | `command_writer` | typed service-team composition command ← `operations.service_team_composition`<br>native service-team identity and membership ← `operations.service_team_lifecycle`<br>registered service-team capability vocabulary ← `operations.service_team_composition`<br>typed geographic scope record ← `gis.spatial_sync` | `owner_managed` | `shadowing` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_service_team_composition.py`<br>`tests/test_team_outbound.py`<br>`tests/test_field_job_chat.py`<br>`tests/services/topology/test_outage_operations.py`<br>`tests/test_workqueue_parity.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.service_team_composition` | explicit service-team routing policy | `command_writer` | typed service-team routing decision ← `operations.service_team_composition`<br>registered service-team routing vocabulary ← `operations.service_team_composition`<br>native service-team identity and membership ← `operations.service_team_lifecycle`<br>registered service-team capability vocabulary ← `operations.service_team_composition`<br>typed geographic scope record ← `gis.spatial_sync` | `owner_managed` | `shadowing` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_service_team_composition.py`<br>`tests/test_team_outbound.py`<br>`tests/test_field_job_chat.py`<br>`tests/services/topology/test_outage_operations.py`<br>`tests/test_workqueue_parity.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.service_team_composition` | set-valued service-team capability, responsibility, and scope resolution | `resolver` | native service-team identity and membership ← `operations.service_team_lifecycle`<br>registered service-team capability vocabulary ← `operations.service_team_composition`<br>typed geographic scope record ← `gis.spatial_sync` | `owner_managed` | `shadowing` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_service_team_composition.py`<br>`tests/test_team_outbound.py`<br>`tests/test_field_job_chat.py`<br>`tests/services/topology/test_outage_operations.py`<br>`tests/test_workqueue_parity.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.service_team_composition` | service-team composition shadow verification | `resolver` | native service-team identity and membership ← `operations.service_team_lifecycle`<br>legacy service-team scalar shadow ← `operations.service_team_source_retirement` | `owner_managed` | `shadowing` | operations administration | `docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_service_team_composition.py`<br>`tests/test_team_outbound.py`<br>`tests/test_field_job_chat.py`<br>`tests/services/topology/test_outage_operations.py`<br>`tests/test_workqueue_parity.py`<br>`tests/architecture/test_service_team_lifecycle_boundary.py` |
| `operations.agent_workqueue` | agent workqueue scope and audience resolution | `resolver` | authenticated staff principal ← `auth.staff_provisioning`<br>native service-team scope ← `operations.service_team_composition` | `coordinator_managed` | `cutover_ready` | support operations | `docs/designs/AGENT_WORKQUEUE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/CRM_WEB_RETIREMENT.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_workqueue_parity.py`<br>`tests/test_workqueue_api.py`<br>`tests/test_workqueue_commands.py`<br>`tests/test_workqueue_web.py`<br>`tests/playwright/e2e/test_workqueue.py`<br>`tests/architecture/test_agent_workqueue_boundary.py` |
| `operations.agent_workqueue` | agent workqueue prioritization projection | `resolver` | native service-team scope ← `operations.service_team_composition`<br>canonical support-ticket state ← `support.ticket_lifecycle`<br>canonical ticket SLA clocks ← `support.ticket_sla_clock`<br>canonical Team Inbox projection ← `communications.team_inbox_projection`<br>native work-order projection ← `operations.work_orders`<br>personal workqueue snooze state ← `operations.agent_workqueue`<br>workqueue scoring policy ← `operations.agent_workqueue` | `coordinator_managed` | `cutover_ready` | support operations | `docs/designs/AGENT_WORKQUEUE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/CRM_WEB_RETIREMENT.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_workqueue_parity.py`<br>`tests/test_workqueue_api.py`<br>`tests/test_workqueue_commands.py`<br>`tests/test_workqueue_web.py`<br>`tests/playwright/e2e/test_workqueue.py`<br>`tests/architecture/test_agent_workqueue_boundary.py` |
| `operations.agent_workqueue` | personal workqueue snooze state | `command_writer` | authenticated staff principal ← `auth.staff_provisioning`<br>scope-checked workqueue action ← `operations.agent_workqueue`<br>personal workqueue snooze state ← `operations.agent_workqueue` | `coordinator_managed` | `cutover_ready` | support operations | `docs/designs/AGENT_WORKQUEUE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/CRM_WEB_RETIREMENT.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_workqueue_parity.py`<br>`tests/test_workqueue_api.py`<br>`tests/test_workqueue_commands.py`<br>`tests/test_workqueue_web.py`<br>`tests/playwright/e2e/test_workqueue.py`<br>`tests/architecture/test_agent_workqueue_boundary.py` |
| `operations.agent_workqueue` | agent workqueue action coordination | `application_coordinator` | authenticated staff principal ← `auth.staff_provisioning`<br>native service-team scope ← `operations.service_team_composition`<br>scope-checked workqueue action ← `operations.agent_workqueue`<br>canonical support-ticket state ← `support.ticket_lifecycle`<br>canonical Team Inbox projection ← `communications.team_inbox_projection`<br>workqueue action idempotency evidence ← `operations.agent_workqueue` | `coordinator_managed` | `cutover_ready` | support operations | `docs/designs/AGENT_WORKQUEUE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/CRM_WEB_RETIREMENT.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_workqueue_parity.py`<br>`tests/test_workqueue_api.py`<br>`tests/test_workqueue_commands.py`<br>`tests/test_workqueue_web.py`<br>`tests/playwright/e2e/test_workqueue.py`<br>`tests/architecture/test_agent_workqueue_boundary.py` |
| `support.ticket_assignment_rule_configuration` | ticket assignment-rule configuration | `authoritative_record` | typed assignment-rule command ← `support.ticket_assignment_rule_configuration`<br>assignment rules ← `support.ticket_assignment_rule_configuration` | `owner_managed` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_assignment_evaluation` | ticket assignment-rule evaluation | `policy` | canonical assignment rules ← `support.ticket_assignment_rule_configuration`<br>ticket assignment facts ← `support.ticket_lifecycle` | `participant` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_assignment_evaluation` | ticket assignment round-robin cursor | `authoritative_record` | canonical assignment rules ← `support.ticket_assignment_rule_configuration`<br>assignment cursor state ← `support.ticket_assignment_evaluation` | `participant` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_automation_rule_configuration` | ticket automation-rule configuration | `authoritative_record` | typed automation-rule command ← `support.ticket_automation_rule_configuration`<br>automation rules ← `support.ticket_automation_rule_configuration` | `owner_managed` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_automation.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_automation_evaluation` | ticket automation-rule evaluation | `policy` | canonical automation rules ← `support.ticket_automation_rule_configuration`<br>ticket automation facts ← `support.ticket_lifecycle` | `read_only` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_automation.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | ticket lifecycle mutations | `command_writer` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | ticket creation and identity | `authoritative_record` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | support ticket human-readable number allocation | `command_writer` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | ticket status vocabulary | `authoritative_record` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | guarded ticket status transitions | `authoritative_record` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | ticket lifecycle timestamps and consequences | `authoritative_record` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | ticket team and person assignment | `authoritative_record` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | ticket comments mentions and attachments | `authoritative_record` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | ticket links duplicates and merges | `authoritative_record` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | signed-link and authenticated resolution confirmation/dispute | `authoritative_record` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | ticket CSAT and satisfaction | `authoritative_record` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_lifecycle` | ticket audit official timeline and transactional events | `authoritative_record` | typed ticket command ← `support.ticket_lifecycle`<br>canonical ticket state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration`<br>customer identity evidence ← `customer.identity_scope`<br>assignment policy proposal ← `support.ticket_assignment_evaluation`<br>automation policy proposal ← `support.ticket_automation_evaluation` | `owner_managed` | `complete` | support operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`tests/test_support_services.py`<br>`tests/test_ticket_status_transition.py`<br>`tests/test_support_automation.py`<br>`tests/test_ticket_assignment_engine.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_configuration` | ticket configuration mutations | `authoritative_record` | typed ticket configuration command ← `support.ticket_configuration`<br>ticket lifecycle vocabulary ← `support.ticket_lifecycle`<br>current ticket configuration ← `support.ticket_configuration` | `owner_managed` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_settings.py`<br>`tests/test_sla_assignment.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_configuration` | operator-visible ticket status subset | `authoritative_record` | typed ticket configuration command ← `support.ticket_configuration`<br>ticket lifecycle vocabulary ← `support.ticket_lifecycle`<br>current ticket configuration ← `support.ticket_configuration` | `owner_managed` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_settings.py`<br>`tests/test_sla_assignment.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_configuration` | ticket priority and type options | `authoritative_record` | typed ticket configuration command ← `support.ticket_configuration`<br>ticket lifecycle vocabulary ← `support.ticket_lifecycle`<br>current ticket configuration ← `support.ticket_configuration` | `owner_managed` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_settings.py`<br>`tests/test_sla_assignment.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_configuration` | ticket routing and priority/type SLA target policy | `authoritative_record` | typed ticket configuration command ← `support.ticket_configuration`<br>ticket lifecycle vocabulary ← `support.ticket_lifecycle`<br>current ticket configuration ← `support.ticket_configuration` | `owner_managed` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_settings.py`<br>`tests/test_sla_assignment.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_sla_clock` | ticket SLA policy assignment | `resolver` | canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>configured ticket SLA targets ← `support.ticket_configuration` | `participant` | `complete` | support operations | `docs/ARCHITECTURE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_sla_assignment.py`<br>`tests/test_operational_sla_policy_ui.py`<br>`tests/architecture/test_operational_sla_policy_ownership.py` |
| `support.ticket_sla_clock` | ticket SLA clock lifecycle | `authoritative_record` | canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>configured ticket SLA targets ← `support.ticket_configuration`<br>current ticket SLA records ← `support.ticket_sla_clock` | `participant` | `complete` | support operations | `docs/ARCHITECTURE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_sla_assignment.py`<br>`tests/test_operational_sla_policy_ui.py`<br>`tests/architecture/test_operational_sla_policy_ownership.py` |
| `support.ticket_sla_clock` | ticket SLA breach records | `authoritative_record` | canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>current ticket SLA records ← `support.ticket_sla_clock` | `participant` | `complete` | support operations | `docs/ARCHITECTURE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_sla_assignment.py`<br>`tests/test_operational_sla_policy_ui.py`<br>`tests/architecture/test_operational_sla_policy_ownership.py` |
| `support.ticket_sla_clock` | ticket SLA breach event emission | `event_policy` | canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>current ticket SLA records ← `support.ticket_sla_clock`<br>configured operational escalation policy ← `operations.sla_escalation` | `participant` | `complete` | support operations | `docs/ARCHITECTURE.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_sla_assignment.py`<br>`tests/test_operational_sla_policy_ui.py`<br>`tests/architecture/test_operational_sla_policy_ownership.py` |
| `support.ticket_work_order_handoff` | ticket-to-work-order issuance eligibility | `application_coordinator` | canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>assigned support-team membership ← `support.ticket_lifecycle`<br>typed issuance request ← `support.ticket_work_order_handoff`<br>work-order command result ← `operations.work_order_commands` | `coordinator_managed` | `complete` | support and field operations | `docs/designs/TICKET_WORK_ORDER_HANDOFF_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/runbooks/TICKET_WORK_ORDER_PROVENANCE_CUTOVER.md`<br>`tests/test_ticket_work_order_handoff.py`<br>`tests/test_ticket_work_order_handoff_migration.py`<br>`tests/architecture/test_ticket_work_order_handoff_boundary.py` |
| `support.ticket_work_order_handoff` | native ticket-to-work-order provenance | `command_writer` | canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>typed issuance request ← `support.ticket_work_order_handoff`<br>work-order command result ← `operations.work_order_commands` | `coordinator_managed` | `complete` | support and field operations | `docs/designs/TICKET_WORK_ORDER_HANDOFF_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/runbooks/TICKET_WORK_ORDER_PROVENANCE_CUTOVER.md`<br>`tests/test_ticket_work_order_handoff.py`<br>`tests/test_ticket_work_order_handoff_migration.py`<br>`tests/architecture/test_ticket_work_order_handoff_boundary.py` |
| `support.ticket_work_order_handoff` | field-outcome projection onto the ticket timeline | `projection_writer` | native ticket-to-work-order provenance ← `support.ticket_work_order_handoff`<br>authoritative field outcome ← `operations.field_completion`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle` | `coordinator_managed` | `complete` | support and field operations | `docs/designs/TICKET_WORK_ORDER_HANDOFF_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/runbooks/TICKET_WORK_ORDER_PROVENANCE_CUTOVER.md`<br>`tests/test_ticket_work_order_handoff.py`<br>`tests/test_ticket_work_order_handoff_migration.py`<br>`tests/architecture/test_ticket_work_order_handoff_boundary.py` |
| `support.ticket_work_order_handoff` | committed field outcome consumption | `application_coordinator` | authoritative field outcome ← `operations.field_completion`<br>native ticket-to-work-order provenance ← `support.ticket_work_order_handoff`<br>receipted owner-output deliveries ← `events.owner_outputs` | `coordinator_managed` | `complete` | support and field operations | `docs/designs/TICKET_WORK_ORDER_HANDOFF_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/runbooks/TICKET_WORK_ORDER_PROVENANCE_CUTOVER.md`<br>`tests/test_ticket_work_order_handoff.py`<br>`tests/test_ticket_work_order_handoff_migration.py`<br>`tests/architecture/test_ticket_work_order_handoff_boundary.py` |
| `support.ticket_bulk_commands` | selected support-ticket bulk membership resolution | `resolver` | typed bulk selection and changes ← `support.ticket_bulk_commands`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration` | `read_only` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_bulk_actions.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_bulk_commands` | support-ticket bulk change normalization | `resolver` | typed bulk selection and changes ← `support.ticket_bulk_commands`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration` | `read_only` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_bulk_actions.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_bulk_commands` | support-ticket bulk update eligibility preview | `policy` | typed bulk selection and changes ← `support.ticket_bulk_commands`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration` | `read_only` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_bulk_actions.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_bulk_commands` | support-ticket bulk confirmation drift detection | `policy` | typed bulk selection and changes ← `support.ticket_bulk_commands`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration` | `read_only` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_bulk_actions.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `support.ticket_bulk_commands` | structured support-ticket bulk update outcomes | `resolver` | typed bulk selection and changes ← `support.ticket_bulk_commands`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration` | `read_only` | `complete` | support operations | `docs/designs/SUPPORT_TICKET_LIFECYCLE_SOT.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_bulk_actions.py`<br>`tests/architecture/test_support_ticket_sot_boundary.py` |
| `ai.gateway` | LLM provider transport | `transport` | assembled advisory prompt ← `ai.generation`<br>resolved provider credential ← `secrets.reference_store` | `not_applicable` | `native` | customer experience platform | `docs/designs/AI_SOT.md`<br>`tests/test_ai_engine.py`<br>`tests/architecture/test_ai_boundaries.py` |
| `ai.gateway` | provider circuit-breaker and endpoint health | `resolver` | observed provider response ← `external:llm_provider` | `not_applicable` | `native` | customer experience platform | `docs/designs/AI_SOT.md`<br>`tests/test_ai_engine.py`<br>`tests/architecture/test_ai_boundaries.py` |
| `operations.service_order_lifecycle` | service-order status transition and recovery lifecycle | `command_writer` | canonical service-order state ← `operations.service_order_lifecycle`<br>service-order transition protocol ← `operations.service_order_lifecycle`<br>recorded administrative recovery evidence ← `customer.accounts` | `owner_managed` | `complete` | service delivery | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_provisioning_services.py`<br>`tests/architecture/test_service_order_status_writers.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `operations.service_order_lifecycle` | verified-implementation provisioning release | `command_writer` | canonical service-order state ← `operations.service_order_lifecycle`<br>verified implementation evidence ← `operations.vendor_project_lifecycle`<br>canonical project lifecycle state ← `operations.project_lifecycle` | `owner_managed` | `complete` | service delivery | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_provisioning_services.py`<br>`tests/architecture/test_service_order_status_writers.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `operations.service_order_lifecycle` | successful-provisioning activation consequence | `command_writer` | canonical service-order state ← `operations.service_order_lifecycle`<br>canonical provisioning result ← `operations.provisioning_workflow`<br>canonical subscription lifecycle state ← `access.subscription_lifecycle` | `owner_managed` | `complete` | service delivery | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_provisioning_services.py`<br>`tests/architecture/test_service_order_status_writers.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `operations.provisioning_lifecycle` | provisioning readiness and activation request decisions | `application_coordinator` | canonical service-order state ← `operations.service_order_lifecycle`<br>canonical provisioning-run outcome ← `operations.provisioning_workflow`<br>native project activation scope ← `operations.project_lifecycle`<br>native field-work completion evidence ← `operations.work_orders`<br>active IP-assignment fact ← `operations.provisioning_context`<br>canonical subscription lifecycle state ← `access.subscription_lifecycle` | `coordinator_managed` | `complete` | service delivery and network operations | `docs/designs/PROVISIONING_LIFECYCLE_SOT.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_provisioning_lifecycle.py`<br>`tests/architecture/test_provisioning_lifecycle_sot.py` |
| `operations.provisioning_lifecycle` | service-order activation confirmation | `application_coordinator` | canonical provisioning-readiness decision ← `operations.provisioning_lifecycle`<br>canonical subscription lifecycle state ← `access.subscription_lifecycle`<br>connectivity projection success observation ← `operations.provisioning_context`<br>service-order transition protocol ← `operations.service_order_lifecycle` | `coordinator_managed` | `complete` | service delivery and network operations | `docs/designs/PROVISIONING_LIFECYCLE_SOT.md`<br>`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_provisioning_lifecycle.py`<br>`tests/architecture/test_provisioning_lifecycle_sot.py` |
| `operations.material_dependencies` | service-work-order material need and operational approval | `command_writer` | canonical service work-order state ← `operations.work_orders`<br>material dependency transition protocol ← `operations.work_order_status`<br>material-support cutover controls ← `control.settings_spec` | `owner_managed` | `cutover_ready` | field operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_field_material_requests.py`<br>`tests/test_dotmac_erp_material_sync.py` |
| `operations.material_dependencies` | backoffice material-outcome projection into the service workflow | `reconciler` | canonical material dependency state ← `operations.material_dependencies`<br>ERP material-support outcome observation ← `external:dotmac_erp`<br>material dependency transition protocol ← `operations.work_order_status` | `owner_managed` | `cutover_ready` | field operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_field_material_requests.py`<br>`tests/test_dotmac_erp_material_sync.py` |
| `operations.material_dependencies` | committed material output consumption | `command_writer` | canonical material dependency state ← `operations.material_dependencies`<br>material dependency transition protocol ← `operations.work_order_status` | `owner_managed` | `cutover_ready` | field operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_field_material_requests.py`<br>`tests/test_dotmac_erp_material_sync.py` |
| `operations.material_dependencies` | work-order material allocation after confirmed external issue | `projection_writer` | canonical material dependency state ← `operations.material_dependencies`<br>ERP material-support outcome observation ← `external:dotmac_erp` | `owner_managed` | `cutover_ready` | field operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_field_material_requests.py`<br>`tests/test_dotmac_erp_material_sync.py` |
| `operations.material_consumption` | field material consumption evidence | `command_writer` | allocated work-order materials ← `operations.material_dependencies` | `owner_managed` | `complete` | field operations | `docs/designs/MATERIALS_VENDOR_ERP_CHAIN.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_field_materials.py`<br>`tests/architecture/test_materials_lifecycle_chain_boundary.py` |
| `operations.project_lifecycle` | Project and ProjectTask identity and lifecycle | `command_writer` | canonical project aggregate ← `operations.project_lifecycle`<br>authorized project command ← `auth.permission_gate` | `owner_managed` | `complete` | service delivery | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_projects_service.py`<br>`tests/test_project_assignment_engine.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `operations.project_lifecycle` | project and task allowed status transitions | `policy` | canonical project aggregate ← `operations.project_lifecycle`<br>project transition protocol ← `operations.project_lifecycle` | `owner_managed` | `complete` | service delivery | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_projects_service.py`<br>`tests/test_project_assignment_engine.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `operations.project_lifecycle` | project and task assignment and scheduling | `command_writer` | canonical project aggregate ← `operations.project_lifecycle`<br>project assignment decision ← `operations.project_assignment_policy`<br>authorized project command ← `auth.permission_gate` | `owner_managed` | `complete` | service delivery | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_projects_service.py`<br>`tests/test_project_assignment_engine.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `operations.project_lifecycle` | project manager assistant manager service-team and task-assignee changes | `command_writer` | canonical project aggregate ← `operations.project_lifecycle`<br>project assignment decision ← `operations.project_assignment_policy`<br>authorized project command ← `auth.permission_gate` | `owner_managed` | `complete` | service delivery | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_projects_service.py`<br>`tests/test_project_assignment_engine.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `operations.project_lifecycle` | Project-to-ProjectTask and project/task-to-work-order relationships | `authoritative_record` | canonical project aggregate ← `operations.project_lifecycle`<br>canonical work-order relationship ← `operations.work_order_commands` | `owner_managed` | `complete` | service delivery | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_projects_service.py`<br>`tests/test_project_assignment_engine.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `operations.project_lifecycle` | project audit records and transactional domain events | `authoritative_record` | canonical project aggregate ← `operations.project_lifecycle`<br>authorized project command ← `auth.permission_gate` | `owner_managed` | `complete` | service delivery | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_projects_service.py`<br>`tests/test_project_assignment_engine.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `operations.project_lifecycle` | project derived-state reconciliation | `reconciler` | canonical project aggregate ← `operations.project_lifecycle` | `owner_managed` | `complete` | service delivery | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_projects_service.py`<br>`tests/test_project_assignment_engine.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `operations.project_assignment_policy` | project assignment-rule evaluation | `policy` | canonical project assignment facts ← `operations.project_lifecycle`<br>configured assignment rules ← `control.settings_spec` | `read_only` | `complete` | service delivery | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`tests/test_project_assignment_engine.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `auth.vendor_user_provisioning` | vendor portal login provisioning and revocation | `command_writer` | vendor portal login command ← `auth.permission_gate` | `owner_managed` | `native` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_vendor_identity.py`<br>`tests/test_vendor_portal_auth.py` |
| `auth.vendor_user_provisioning` | vendor organisation role assignment | `command_writer` | vendor portal login command ← `auth.permission_gate` | `owner_managed` | `native` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_vendor_identity.py`<br>`tests/test_vendor_portal_auth.py` |
| `operations.installation_scope` | idempotent structural InstallationProject root creation | `command_writer` | canonical native project state ← `operations.project_lifecycle`<br>installation scope creation command ← `sales.orders` | `participant` | `complete` | service delivery | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_sales_orders_services.py`<br>`tests/test_sot_relationships.py` |
| `operations.installation_scope` | Project-to-InstallationProject subscriber alignment | `policy` | canonical native project state ← `operations.project_lifecycle`<br>installation scope creation command ← `sales.orders` | `participant` | `complete` | service delivery | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_sales_orders_services.py`<br>`tests/test_sot_relationships.py` |
| `operations.installation_scope` | buildout-rooted installation scope creation | `command_writer` | canonical native project state ← `operations.project_lifecycle` | `participant` | `complete` | service delivery | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_sales_orders_services.py`<br>`tests/test_sot_relationships.py` |
| `operations.vendor_material_release` | vendor project material release need and approval | `command_writer` | canonical installation-project lifecycle state ← `operations.vendor_project_lifecycle` | `participant` | `native` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/BACKOFFICE_INTEGRATION_BOUNDARY.md`<br>`tests/test_vendor_supply.py` |
| `operations.vendor_material_release` | backoffice material issue outcome projection for vendors | `reconciler` | backoffice material issue outcome ← `integration.dotmac_erp_material_support_adapter` | `participant` | `native` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/BACKOFFICE_INTEGRATION_BOUNDARY.md`<br>`tests/test_vendor_supply.py` |
| `operations.vendor_advances` | vendor advance eligibility, ceiling, and approval | `command_writer` | canonical installation-project lifecycle state ← `operations.vendor_project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records`<br>vendor advance cap policy ← `control.settings_spec` | `participant` | `native` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/BACKOFFICE_INTEGRATION_BOUNDARY.md`<br>`tests/test_vendor_supply.py` |
| `operations.vendor_advances` | payables settlement observation for vendor advances | `reconciler` | vendor payables settlement observation ← `integration.dotmac_erp_payables_adapter` | `participant` | `native` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/BACKOFFICE_INTEGRATION_BOUNDARY.md`<br>`tests/test_vendor_supply.py` |
| `operations.vendor_project_lifecycle` | vendor start/complete and staff verify/rework installation-project transitions | `command_writer` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>authenticated assigned-vendor transition evidence ← `auth.permission_gate`<br>vendor lifecycle transition protocol ← `operations.vendor_project_lifecycle`<br>work-order as-built evidence policy ← `operations.work_order_commands` | `participant` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_lifecycle.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_project_lifecycle_boundary.py` |
| `operations.vendor_project_lifecycle` | staff bidding publication and direct vendor assignment | `command_writer` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>vendor lifecycle transition protocol ← `operations.vendor_project_lifecycle` | `participant` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_lifecycle.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_project_lifecycle_boundary.py` |
| `operations.vendor_project_lifecycle` | durable vendor lifecycle actor/time/reason/event evidence | `authoritative_record` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>authenticated assigned-vendor transition evidence ← `auth.permission_gate`<br>work-order as-built evidence policy ← `operations.work_order_commands` | `participant` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_lifecycle.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_project_lifecycle_boundary.py` |
| `operations.vendor_project_lifecycle` | typed vendor project lifecycle outbox events | `authoritative_record` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>authenticated assigned-vendor transition evidence ← `auth.permission_gate` | `participant` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_lifecycle.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_project_lifecycle_boundary.py` |
| `operations.vendor_project_workspace` | vendor project workspace read and action projections | `resolver` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py`<br>`tests/test_vendor_action_eligibility.py` |
| `operations.vendor_project_workspace` | vendor project workspace mutation coordination | `application_coordinator` | authenticated vendor workspace command context ← `auth.permission_gate`<br>canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records`<br>canonical vendor material release decisions ← `operations.vendor_material_release`<br>canonical vendor advance decisions ← `operations.vendor_advances`<br>vendor quote currency and validity policy ← `control.settings_spec`<br>vendor workspace mutation protocol ← `operations.vendor_project_workspace` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py`<br>`tests/test_vendor_action_eligibility.py` |
| `operations.vendor_project_workspace` | quote creation eligibility | `policy` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py`<br>`tests/test_vendor_action_eligibility.py` |
| `operations.vendor_project_workspace` | quote submission eligibility and impact snapshot | `policy` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records`<br>vendor workspace mutation protocol ← `operations.vendor_project_workspace` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py`<br>`tests/test_vendor_action_eligibility.py` |
| `operations.vendor_project_workspace` | as-built submission eligibility and impact snapshot | `policy` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records`<br>vendor workspace mutation protocol ← `operations.vendor_project_workspace` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py`<br>`tests/test_vendor_action_eligibility.py` |
| `operations.vendor_project_workspace` | staff project-review eligibility and impact snapshot | `policy` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records`<br>work-order as-built evidence policy ← `operations.work_order_commands`<br>vendor workspace mutation protocol ← `operations.vendor_project_workspace` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py`<br>`tests/test_vendor_action_eligibility.py` |
| `operations.vendor_project_workspace` | staff proposed-route review eligibility and impact snapshot | `policy` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records`<br>vendor workspace mutation protocol ← `operations.vendor_project_workspace` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py`<br>`tests/test_vendor_action_eligibility.py` |
| `operations.vendor_project_workspace` | staff as-built-review eligibility and impact snapshot | `policy` | canonical installation-project lifecycle state ← `operations.project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records`<br>vendor workspace mutation protocol ← `operations.vendor_project_workspace` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_project_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py`<br>`tests/test_vendor_action_eligibility.py` |
| `operations.vendor_project_records` | vendor installation-project quote lifecycle | `command_writer` | validated vendor project record transition ← `operations.vendor_project_workspace`<br>canonical installation-project lifecycle state ← `operations.project_lifecycle` | `participant` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_route_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `operations.vendor_project_records` | proposed vendor route-revision lifecycle | `command_writer` | validated vendor project record transition ← `operations.vendor_project_workspace`<br>canonical installation-project lifecycle state ← `operations.project_lifecycle` | `participant` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_route_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `operations.vendor_project_records` | staff proposed-route review state and immutable evidence | `authoritative_record` | validated vendor project record transition ← `operations.vendor_project_workspace`<br>canonical installation-project lifecycle state ← `operations.project_lifecycle` | `participant` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_route_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `operations.vendor_project_records` | vendor as-built route and line-item lifecycle | `authoritative_record` | validated vendor project record transition ← `operations.vendor_project_workspace`<br>canonical installation-project lifecycle state ← `operations.project_lifecycle` | `participant` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_route_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `operations.vendor_project_records` | staff as-built review state and immutable evidence | `authoritative_record` | validated vendor project record transition ← `operations.vendor_project_workspace`<br>canonical installation-project lifecycle state ← `operations.project_lifecycle` | `participant` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/VENDOR_ROUTE_REVISION_AUTHORING.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_workspace.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/test_vendor_route_review.py`<br>`tests/test_vendor_as_built_review.py`<br>`tests/test_vendor_route_revision_authoring.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `operations.vendor_purchase_invoices` | vendor purchase-invoice read and action projections | `resolver` | canonical vendor purchase-invoice records ← `operations.vendor_purchase_invoice_records`<br>canonical installation-project lifecycle state ← `operations.vendor_project_lifecycle` | `coordinator_managed` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_phase5_vendor_purchase_invoices.py`<br>`tests/test_vendor_payment_visibility.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_purchase_invoice_boundary.py` |
| `operations.vendor_purchase_invoices` | vendor purchase-invoice mutation coordination | `application_coordinator` | authenticated purchase-invoice command context ← `auth.permission_gate`<br>canonical vendor purchase-invoice records ← `operations.vendor_purchase_invoice_records`<br>canonical installation-project lifecycle state ← `operations.vendor_project_lifecycle`<br>purchase-invoice currency policy ← `control.settings_spec`<br>purchase-invoice mutation protocol ← `operations.vendor_purchase_invoices` | `coordinator_managed` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_phase5_vendor_purchase_invoices.py`<br>`tests/test_vendor_payment_visibility.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_purchase_invoice_boundary.py` |
| `operations.vendor_purchase_invoices` | purchase-invoice submission eligibility and financial preview | `policy` | canonical vendor purchase-invoice records ← `operations.vendor_purchase_invoice_records`<br>canonical installation-project lifecycle state ← `operations.vendor_project_lifecycle`<br>purchase-invoice mutation protocol ← `operations.vendor_purchase_invoices` | `coordinator_managed` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_phase5_vendor_purchase_invoices.py`<br>`tests/test_vendor_payment_visibility.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_purchase_invoice_boundary.py` |
| `operations.vendor_purchase_invoices` | vendor-facing payables-status observation projection | `resolver` | canonical vendor purchase-invoice records ← `operations.vendor_purchase_invoice_records`<br>timestamped ERP accounts-payable observation ← `integration.dotmac_erp_payables_adapter`<br>UI payment-state projection vocabulary ← `ui.projection_contracts` | `coordinator_managed` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_phase5_vendor_purchase_invoices.py`<br>`tests/test_vendor_payment_visibility.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_purchase_invoice_boundary.py` |
| `operations.vendor_purchase_invoice_records` | vendor purchase-invoice lifecycle | `command_writer` | validated purchase-invoice transition ← `operations.vendor_purchase_invoices`<br>canonical installation-project lifecycle state ← `operations.vendor_project_lifecycle` | `participant` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_phase5_vendor_purchase_invoices.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_purchase_invoice_boundary.py` |
| `operations.vendor_purchase_invoice_records` | vendor purchase-invoice line-item lifecycle | `command_writer` | validated purchase-invoice transition ← `operations.vendor_purchase_invoices`<br>canonical installation-project lifecycle state ← `operations.vendor_project_lifecycle` | `participant` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_phase5_vendor_purchase_invoices.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_purchase_invoice_boundary.py` |
| `operations.vendor_purchase_invoice_records` | purchase-invoice attachment and ERP request evidence | `authoritative_record` | validated purchase-invoice transition ← `operations.vendor_purchase_invoices`<br>canonical installation-project lifecycle state ← `operations.vendor_project_lifecycle` | `participant` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_phase5_vendor_purchase_invoices.py`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_purchase_invoice_boundary.py` |
| `operations.vendor_submission_confirmation` | short-lived signed vendor submission proposal | `policy` | authenticated vendor principal context ← `auth.permission_gate`<br>vendor project workspace submission preview ← `operations.vendor_project_workspace`<br>vendor project lifecycle submission preview ← `operations.vendor_project_lifecycle`<br>vendor purchase-invoice submission preview ← `operations.vendor_purchase_invoices`<br>capability signing envelope ← `auth.token_signing`<br>vendor confirmation protocol invariants ← `operations.vendor_submission_confirmation` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_submission_confirmation_boundary.py`<br>`tests/test_vendor_lifecycle.py` |
| `operations.vendor_submission_confirmation` | vendor submission stale-preview verification | `policy` | authenticated vendor principal context ← `auth.permission_gate`<br>vendor project workspace submission preview ← `operations.vendor_project_workspace`<br>vendor project lifecycle submission preview ← `operations.vendor_project_lifecycle`<br>vendor purchase-invoice submission preview ← `operations.vendor_purchase_invoices`<br>capability signing envelope ← `auth.token_signing` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_submission_confirmation_boundary.py`<br>`tests/test_vendor_lifecycle.py` |
| `operations.vendor_submission_confirmation` | vendor submission idempotency and replay result | `application_coordinator` | authenticated vendor principal context ← `auth.permission_gate`<br>vendor project workspace submission preview ← `operations.vendor_project_workspace`<br>vendor project lifecycle submission preview ← `operations.vendor_project_lifecycle`<br>vendor purchase-invoice submission preview ← `operations.vendor_purchase_invoices`<br>capability signing envelope ← `auth.token_signing`<br>canonical vendor submission replay record ← `operations.vendor_submission_confirmation` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_submission_proposals.py`<br>`tests/architecture/test_vendor_submission_confirmation_boundary.py`<br>`tests/test_vendor_lifecycle.py` |
| `operations.vendor_supply_review_confirmation` | short-lived signed vendor supply review proposal | `policy` | authenticated vendor supply review context ← `auth.permission_gate`<br>canonical vendor supply review preview ← `ui.vendor_supply_projection`<br>capability signing envelope ← `auth.token_signing` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_supply_ui.py`<br>`tests/architecture/test_vendor_supply_ui_boundary.py`<br>`tests/test_vendor_delivery_portfolio.py`<br>`tests/architecture/test_vendor_delivery_portfolio_boundary.py` |
| `operations.vendor_supply_review_confirmation` | vendor supply review stale-preview verification | `policy` | canonical vendor supply review preview ← `ui.vendor_supply_projection`<br>capability signing envelope ← `auth.token_signing` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_supply_ui.py`<br>`tests/architecture/test_vendor_supply_ui_boundary.py`<br>`tests/test_vendor_delivery_portfolio.py`<br>`tests/architecture/test_vendor_delivery_portfolio_boundary.py` |
| `operations.vendor_supply_review_confirmation` | vendor supply review idempotency and replay result | `application_coordinator` | authenticated vendor supply review context ← `auth.permission_gate`<br>canonical vendor supply review preview ← `ui.vendor_supply_projection`<br>vendor supply review replay record ← `operations.vendor_supply_review_confirmation` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_supply_ui.py`<br>`tests/architecture/test_vendor_supply_ui_boundary.py`<br>`tests/test_vendor_delivery_portfolio.py`<br>`tests/architecture/test_vendor_delivery_portfolio_boundary.py` |
| `operations.vendor_project_review_confirmation` | short-lived signed staff project-review proposal | `policy` | authenticated staff review context ← `auth.permission_gate`<br>canonical staff project-review preview ← `operations.vendor_project_workspace`<br>capability signing envelope ← `auth.token_signing`<br>staff project-review confirmation protocol ← `operations.vendor_project_review_confirmation` | `coordinator_managed` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_review.py`<br>`tests/architecture/test_vendor_project_lifecycle_boundary.py` |
| `operations.vendor_project_review_confirmation` | staff project-review stale-preview verification | `policy` | authenticated staff review context ← `auth.permission_gate`<br>canonical staff project-review preview ← `operations.vendor_project_workspace`<br>capability signing envelope ← `auth.token_signing` | `coordinator_managed` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_review.py`<br>`tests/architecture/test_vendor_project_lifecycle_boundary.py` |
| `operations.vendor_project_review_confirmation` | staff project-review idempotency and replay result | `application_coordinator` | authenticated staff review context ← `auth.permission_gate`<br>canonical staff project-review preview ← `operations.vendor_project_workspace`<br>capability signing envelope ← `auth.token_signing`<br>canonical staff project-review replay record ← `operations.vendor_project_review_confirmation` | `coordinator_managed` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_project_review.py`<br>`tests/architecture/test_vendor_project_lifecycle_boundary.py` |
| `operations.vendor_route_review_confirmation` | short-lived signed staff proposed-route review proposal | `policy` | authenticated staff proposed-route review context ← `auth.permission_gate`<br>canonical staff proposed-route review preview ← `operations.vendor_project_workspace`<br>capability signing envelope ← `auth.token_signing`<br>staff proposed-route review confirmation protocol ← `operations.vendor_route_review_confirmation` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_route_review.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `operations.vendor_route_review_confirmation` | staff proposed-route review stale-preview verification | `policy` | authenticated staff proposed-route review context ← `auth.permission_gate`<br>canonical staff proposed-route review preview ← `operations.vendor_project_workspace`<br>capability signing envelope ← `auth.token_signing` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_route_review.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `operations.vendor_route_review_confirmation` | staff proposed-route review idempotency and replay result | `application_coordinator` | authenticated staff proposed-route review context ← `auth.permission_gate`<br>canonical staff proposed-route review preview ← `operations.vendor_project_workspace`<br>capability signing envelope ← `auth.token_signing`<br>canonical staff proposed-route review replay record ← `operations.vendor_route_review_confirmation` | `coordinator_managed` | `complete` | vendor operations | `docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_route_review.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `operations.vendor_as_built_review_confirmation` | short-lived signed staff as-built review proposal | `policy` | authenticated staff as-built review context ← `auth.permission_gate`<br>canonical staff as-built review preview ← `operations.vendor_project_workspace`<br>capability signing envelope ← `auth.token_signing`<br>staff as-built review confirmation protocol ← `operations.vendor_as_built_review_confirmation` | `coordinator_managed` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_as_built_review.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `operations.vendor_as_built_review_confirmation` | staff as-built review stale-preview verification | `policy` | authenticated staff as-built review context ← `auth.permission_gate`<br>canonical staff as-built review preview ← `operations.vendor_project_workspace`<br>capability signing envelope ← `auth.token_signing` | `coordinator_managed` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_as_built_review.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `operations.vendor_as_built_review_confirmation` | staff as-built review idempotency and replay result | `application_coordinator` | authenticated staff as-built review context ← `auth.permission_gate`<br>canonical staff as-built review preview ← `operations.vendor_project_workspace`<br>capability signing envelope ← `auth.token_signing`<br>canonical staff as-built review replay record ← `operations.vendor_as_built_review_confirmation` | `coordinator_managed` | `complete` | vendor operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_vendor_as_built_review.py`<br>`tests/architecture/test_vendor_project_workspace_boundary.py` |
| `auth.subscriber_assignments` | subscriber role and direct-permission assignments | `command_writer` | authorized subscriber assignment principal ← `auth.permission_gate`<br>active role and permission catalog ← `auth.rbac_catalog`<br>canonical subscriber assignment state ← `auth.subscriber_assignments` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_subscriber_assignments.py`<br>`tests/architecture/test_subscriber_assignment_boundary.py` |
| `auth.rbac_catalog` | role catalog and role-permission policy | `command_writer` | authorized RBAC catalog principal ← `auth.permission_gate`<br>canonical role and role-permission catalog ← `auth.rbac_catalog`<br>system-user role grant references ← `auth.system_user_assignments`<br>subscriber role grant references ← `auth.subscriber_assignments` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_rbac_catalog_owner.py`<br>`tests/architecture/test_rbac_catalog_boundary.py` |
| `auth.rbac_catalog` | permission catalog | `command_writer` | authorized RBAC catalog principal ← `auth.permission_gate`<br>canonical permission catalog ← `auth.rbac_catalog`<br>system-user permission grant references ← `auth.system_user_assignments`<br>subscriber permission grant references ← `auth.subscriber_assignments` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_rbac_catalog_owner.py`<br>`tests/architecture/test_rbac_catalog_boundary.py` |
| `auth.system_user_assignments` | system-user role and direct-permission assignments | `command_writer` | authorized system-user assignment principal ← `auth.permission_gate`<br>active role and permission catalog ← `auth.rbac_catalog`<br>canonical system-user assignment state ← `auth.system_user_assignments` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_system_user_assignments.py`<br>`tests/architecture/test_system_user_assignment_boundary.py` |
| `auth.system_user_assignments` | source-scoped managed system-user role convergence | `command_writer` | active role and permission catalog ← `auth.rbac_catalog`<br>canonical system-user assignment state ← `auth.system_user_assignments` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_system_user_assignments.py`<br>`tests/architecture/test_system_user_assignment_boundary.py` |
| `auth.access_invitations` | access invitation lifecycle | `command_writer` | issued invitation capabilities ← `auth.credential_recovery` | `owner_managed` | `complete` | platform security | `docs/designs/IDENTITY_ONBOARDING_CHAIN.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_access_invitations.py`<br>`tests/architecture/test_identity_onboarding_chain_boundary.py` |
| `auth.credential_recovery` | password recovery request and delivery intent | `command_writer` | credential recovery command evidence ← `auth.credential_recovery`<br>canonical recoverable principal state ← `auth.credential_recovery`<br>credential recovery policy settings ← `control.settings_spec`<br>durable recovery delivery boundary ← `communications.intents` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_credential_recovery.py`<br>`tests/architecture/test_credential_recovery_boundary.py` |
| `auth.credential_recovery` | password reset credential transition | `command_writer` | credential recovery command evidence ← `auth.credential_recovery`<br>canonical recoverable principal state ← `auth.credential_recovery`<br>credential recovery policy settings ← `control.settings_spec`<br>verified recovery capability envelope ← `auth.token_signing` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_credential_recovery.py`<br>`tests/architecture/test_credential_recovery_boundary.py` |
| `auth.credential_recovery` | credential recovery session projection invalidation | `reconciler` | canonical recoverable principal state ← `auth.credential_recovery` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_credential_recovery.py`<br>`tests/architecture/test_credential_recovery_boundary.py` |
| `auth.customer_credential_enrollment` | credential enrollment delivery request | `command_writer` | credential enrollment command evidence ← `auth.customer_credential_enrollment`<br>canonical referral account context ← `referrals.account_conversion`<br>canonical customer credential state ← `auth.customer_credential_enrollment`<br>credential enrollment policy settings ← `control.settings_spec`<br>durable enrollment delivery intent ← `communications.intents` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/REFERRAL_CREDENTIAL_ENROLLMENT.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referral_credential_enrollment.py`<br>`tests/architecture/test_customer_credential_enrollment_boundary.py` |
| `auth.customer_credential_enrollment` | referral-created customer local credential enrollment | `command_writer` | credential enrollment command evidence ← `auth.customer_credential_enrollment`<br>canonical referral account context ← `referrals.account_conversion`<br>canonical customer credential state ← `auth.customer_credential_enrollment`<br>credential enrollment policy settings ← `control.settings_spec`<br>verified enrollment capability envelope ← `auth.token_signing` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/REFERRAL_CREDENTIAL_ENROLLMENT.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referral_credential_enrollment.py`<br>`tests/architecture/test_customer_credential_enrollment_boundary.py` |
| `auth.customer_credential_enrollment` | credential enrollment capability purpose claims and lifetime | `policy` | canonical referral account context ← `referrals.account_conversion`<br>canonical customer credential state ← `auth.customer_credential_enrollment`<br>credential enrollment policy settings ← `control.settings_spec`<br>verified enrollment capability envelope ← `auth.token_signing` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/REFERRAL_CREDENTIAL_ENROLLMENT.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referral_credential_enrollment.py`<br>`tests/architecture/test_customer_credential_enrollment_boundary.py` |
| `auth.customer_credential_enrollment` | single-use enrollment and account email verification consequence | `command_writer` | credential enrollment command evidence ← `auth.customer_credential_enrollment`<br>canonical customer credential state ← `auth.customer_credential_enrollment`<br>verified enrollment capability envelope ← `auth.token_signing` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/REFERRAL_CREDENTIAL_ENROLLMENT.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referral_credential_enrollment.py`<br>`tests/architecture/test_customer_credential_enrollment_boundary.py` |
| `auth.customer_credential_enrollment` | credential enrollment authentication cache projection | `reconciler` | canonical customer credential state ← `auth.customer_credential_enrollment` | `owner_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/REFERRAL_CREDENTIAL_ENROLLMENT.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referral_credential_enrollment.py`<br>`tests/architecture/test_customer_credential_enrollment_boundary.py` |
| `auth.staff_provisioning` | staff account provisioning | `application_coordinator` | ERP HR staff lifecycle request ← `external:dotmac_erp`<br>authorized RBAC assignment principal ← `auth.permission_gate`<br>active role catalog ← `auth.rbac_catalog`<br>managed role grant state ← `auth.system_user_assignments`<br>canonical staff identity and credential state ← `auth.staff_provisioning`<br>canonical Person Party identity ← `party.registry` | `coordinator_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_api_staff_sync.py`<br>`tests/test_staff_provisioning_owner.py`<br>`tests/architecture/test_staff_provisioning_boundary.py` |
| `auth.staff_provisioning` | staff identity bootstrap | `command_writer` | ERP HR staff lifecycle request ← `external:dotmac_erp`<br>canonical staff identity and credential state ← `auth.staff_provisioning`<br>canonical Person Party identity ← `party.registry` | `coordinator_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_api_staff_sync.py`<br>`tests/test_staff_provisioning_owner.py`<br>`tests/architecture/test_staff_provisioning_boundary.py` |
| `auth.reseller_onboarding` | reseller portal principal onboarding | `application_coordinator` | authorized reseller onboarding principal ← `auth.permission_gate`<br>canonical reseller and subscriber account state ← `customer.accounts`<br>canonical subscriber assignment state ← `auth.subscriber_assignments`<br>reseller principal cutover gate ← `control.feature_registry`<br>canonical reseller onboarding state ← `auth.reseller_onboarding` | `coordinator_managed` | `complete` | platform security | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_reseller_onboarding.py`<br>`tests/architecture/test_reseller_onboarding_boundary.py` |
| `access.event_policy` | event-driven enforcement feature policy | `event_policy` | canonical RADIUS event settings ← `control.settings_spec` | `read_only` | `complete` | network access | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_enforcement_event_policy.py`<br>`tests/test_events_enforcement_services.py`<br>`tests/test_radius_shadow_handler_integration.py`<br>`tests/architecture/test_enforcement_event_policy_boundary.py` |
| `access.event_policy` | FUP enforcement action settings | `event_policy` | canonical FUP event settings ← `control.settings_spec`<br>usage-exhausted action evidence ← `access.fup_enforcement_sweep` | `read_only` | `complete` | network access | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_enforcement_event_policy.py`<br>`tests/test_events_enforcement_services.py`<br>`tests/test_radius_shadow_handler_integration.py`<br>`tests/architecture/test_enforcement_event_policy_boundary.py` |
| `access.walled_garden_policy` | captive account eligibility | `policy` | canonical subscriber access identity ← `customer.accounts`<br>canonical reseller scope ← `customer.identity_scope`<br>captive restriction protocol ← `access.walled_garden_policy` | `read_only` | `complete` | network access | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_walled_garden_policy.py`<br>`tests/test_radius_shadow_handler_integration.py`<br>`tests/architecture/test_grace_walled_garden_ownership.py`<br>`tests/architecture/test_walled_garden_policy_boundary.py` |
| `access.walled_garden_policy` | captive network readiness | `policy` | canonical captive network settings ← `control.settings_spec`<br>captive restriction protocol ← `access.walled_garden_policy` | `read_only` | `complete` | network access | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_walled_garden_policy.py`<br>`tests/test_radius_shadow_handler_integration.py`<br>`tests/architecture/test_grace_walled_garden_ownership.py`<br>`tests/architecture/test_walled_garden_policy_boundary.py` |
| `access.walled_garden_policy` | effective hard-reject/captive restriction | `policy` | canonical subscriber access identity ← `customer.accounts`<br>canonical reseller scope ← `customer.identity_scope`<br>canonical captive network settings ← `control.settings_spec`<br>canonical enforcement locks ← `access.subscription_lifecycle`<br>captive restriction protocol ← `access.walled_garden_policy` | `read_only` | `complete` | network access | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_walled_garden_policy.py`<br>`tests/test_radius_shadow_handler_integration.py`<br>`tests/architecture/test_grace_walled_garden_ownership.py`<br>`tests/architecture/test_walled_garden_policy_boundary.py` |
| `access.walled_garden_policy` | most-restrictive-active-lock resolution | `resolver` | canonical subscription lifecycle state ← `access.subscription_lifecycle`<br>canonical enforcement locks ← `access.subscription_lifecycle`<br>captive restriction protocol ← `access.walled_garden_policy` | `read_only` | `complete` | network access | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/audits/BILLING_SOT_AUDIT_2026-07-12.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_walled_garden_policy.py`<br>`tests/test_radius_shadow_handler_integration.py`<br>`tests/architecture/test_grace_walled_garden_ownership.py`<br>`tests/architecture/test_walled_garden_policy_boundary.py` |
| `access.fup_rule_engine` | FUP policy and rule definitions (CRUD) | `command_writer` | authenticated FUP policy command context ← `auth.permission_gate`<br>canonical catalog offer ← `service_intent.catalog_policy`<br>FUP policy mutation protocol ← `access.fup_rule_engine` | `owner_managed` | `complete` | network access | `docs/designs/FUP_CONSUMPTION_WINDOWS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_fup_ui_gaps.py`<br>`tests/test_fup_period_aware_evaluation.py`<br>`tests/test_fup_submonthly_safeguards.py`<br>`tests/architecture/test_fup_rule_engine_boundary.py` |
| `access.fup_rule_engine` | FUP rule evaluation and simulation | `policy` | canonical FUP policy and rule definitions ← `access.fup_rule_engine`<br>period-scoped FUP usage observations ← `access.fup_usage_windows`<br>FUP rule evaluation protocol ← `access.fup_rule_engine` | `owner_managed` | `complete` | network access | `docs/designs/FUP_CONSUMPTION_WINDOWS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_fup_ui_gaps.py`<br>`tests/test_fup_period_aware_evaluation.py`<br>`tests/test_fup_submonthly_safeguards.py`<br>`tests/architecture/test_fup_rule_engine_boundary.py` |
| `access.fup_runtime_state` | FUP per-subscription runtime state rows | `projection_writer` | canonical subscription offer state ← `access.subscription_lifecycle`<br>resolved FUP enforcement consequence ← `access.fup_enforcement_sweep`<br>applied access consequence evidence ← `access.session_enforcement` | `participant` | `complete` | network access | `docs/designs/FUP_CONSUMPTION_WINDOWS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_fup_runtime_state_owner.py`<br>`tests/architecture/test_fup_runtime_state_boundary.py`<br>`tests/test_fup_lift_enforcement.py`<br>`tests/test_fup_evaluate_commits.py` |
| `access.fup_usage_windows` | FUP consumption window bounds | `resolver` | FUP consumption period policy ← `access.fup_usage_windows` | `read_only` | `complete` | network access | `docs/designs/FUP_CONSUMPTION_WINDOWS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_fup_window_bounds.py`<br>`tests/test_fup_usage_reader.py` |
| `access.fup_usage_windows` | windowed FUP usage aggregation | `resolver` | FUP consumption period policy ← `access.fup_usage_windows`<br>rated quota and session usage facts ← `sessions.radius_reconciliation` | `read_only` | `complete` | network access | `docs/designs/FUP_CONSUMPTION_WINDOWS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_fup_window_bounds.py`<br>`tests/test_fup_usage_reader.py` |
| `access.fup_enforcement_sweep` | FUP sweep enforce/warn/reset decisions | `application_coordinator` | canonical subscription offer state ← `access.subscription_lifecycle`<br>canonical FUP rule decisions ← `access.fup_rule_engine`<br>period-scoped FUP usage observations ← `access.fup_usage_windows`<br>canonical FUP runtime state ← `access.fup_runtime_state`<br>FUP enforcement control settings ← `control.settings_spec`<br>FUP sweep command protocol ← `access.fup_enforcement_sweep` | `coordinator_managed` | `complete` | network access | `docs/designs/FUP_CONSUMPTION_WINDOWS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_fup_evaluate_commits.py`<br>`tests/test_fup_enforcement_hardening.py`<br>`tests/test_fup_hysteresis.py`<br>`tests/test_fup_notifications.py`<br>`tests/architecture/test_fup_enforcement_boundary.py` |
| `access.fup_enforcement_sweep` | FUP enforcement transition and cooldown hysteresis | `policy` | canonical FUP rule decisions ← `access.fup_rule_engine`<br>canonical FUP runtime state ← `access.fup_runtime_state`<br>FUP sweep command protocol ← `access.fup_enforcement_sweep` | `coordinator_managed` | `complete` | network access | `docs/designs/FUP_CONSUMPTION_WINDOWS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_fup_evaluate_commits.py`<br>`tests/test_fup_enforcement_hardening.py`<br>`tests/test_fup_hysteresis.py`<br>`tests/test_fup_notifications.py`<br>`tests/architecture/test_fup_enforcement_boundary.py` |
| `access.fup_enforcement_sweep` | FUP repeat-upsell nudge policy | `policy` | canonical FUP rule decisions ← `access.fup_rule_engine`<br>canonical FUP notification history ← `communications.notification_service`<br>period-scoped FUP usage observations ← `access.fup_usage_windows` | `coordinator_managed` | `complete` | network access | `docs/designs/FUP_CONSUMPTION_WINDOWS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_fup_evaluate_commits.py`<br>`tests/test_fup_enforcement_hardening.py`<br>`tests/test_fup_hysteresis.py`<br>`tests/test_fup_notifications.py`<br>`tests/architecture/test_fup_enforcement_boundary.py` |
| `access.fup_enforcement_sweep` | FUP customer notification fan-out | `policy` | resolved FUP enforcement decision ← `access.fup_enforcement_sweep`<br>FUP communication channel policy ← `communications.notification_service` | `coordinator_managed` | `complete` | network access | `docs/designs/FUP_CONSUMPTION_WINDOWS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`tests/test_fup_evaluate_commits.py`<br>`tests/test_fup_enforcement_hardening.py`<br>`tests/test_fup_hysteresis.py`<br>`tests/test_fup_notifications.py`<br>`tests/architecture/test_fup_enforcement_boundary.py` |
| `service_intent.subscription_change_execution` | relocation charge evidence and settlement admission | `application_coordinator` | confirmed relocation quote evidence ← `service_intent.subscription_lifecycle_execution`<br>canonical invoice and payment allocation evidence ← `financial.payments` | `coordinator_managed` | `complete` | customer service delivery, billing, and network operations | `docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md`<br>`docs/designs/PROVISIONING_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_customer_plan_change_prepaid.py`<br>`tests/architecture/test_provisioning_lifecycle_sot.py` |
| `service_intent.subscription_change_execution` | paid relocation fulfillment release | `application_coordinator` | canonical invoice and payment allocation evidence ← `financial.payments`<br>canonical subscription-change execution state ← `service_intent.subscription_change_execution` | `coordinator_managed` | `complete` | customer service delivery, billing, and network operations | `docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md`<br>`docs/designs/PROVISIONING_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_customer_plan_change_prepaid.py`<br>`tests/architecture/test_provisioning_lifecycle_sot.py` |
| `service_intent.subscription_change_execution` | remote reprovision verification | `application_coordinator` | catalog-linked target RADIUS profile ← `service_intent.subscription_lifecycle_execution`<br>canonical RADIUS profile observation ← `access.radius_state`<br>canonical subscription-change execution state ← `service_intent.subscription_change_execution` | `coordinator_managed` | `complete` | customer service delivery, billing, and network operations | `docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md`<br>`docs/designs/PROVISIONING_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_customer_plan_change_prepaid.py`<br>`tests/architecture/test_provisioning_lifecycle_sot.py` |
| `service_intent.subscription_change_execution` | verified service-change finalization | `application_coordinator` | canonical provisioning-readiness decision ← `operations.provisioning_lifecycle`<br>canonical subscription-change execution state ← `service_intent.subscription_change_execution` | `coordinator_managed` | `complete` | customer service delivery, billing, and network operations | `docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md`<br>`docs/designs/PROVISIONING_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_customer_plan_change_prepaid.py`<br>`tests/architecture/test_provisioning_lifecycle_sot.py` |
| `service_intent.subscription_change_execution` | interrupted execution-chain reconciliation | `application_coordinator` | canonical invoice and payment allocation evidence ← `financial.payments`<br>canonical RADIUS profile observation ← `access.radius_state`<br>canonical subscription-change execution state ← `service_intent.subscription_change_execution`<br>canonical provisioning-readiness decision ← `operations.provisioning_lifecycle` | `coordinator_managed` | `complete` | customer service delivery, billing, and network operations | `docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md`<br>`docs/designs/PROVISIONING_LIFECYCLE_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_customer_plan_change_prepaid.py`<br>`tests/architecture/test_provisioning_lifecycle_sot.py` |
| `integration.installations` | version-pinned integration installation lifecycle | `command_writer` | deployed connector manifest ← `integration.registry`<br>integration installation protocol ← `integration.installations`<br>canonical integration installation aggregate ← `integration.installations` | `owner_managed` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_installations.py`<br>`tests/test_integration_installation_api.py`<br>`tests/test_integration_manifest_deployment_gate.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.installations` | explicit integration manifest adoption | `command_writer` | deployed connector manifest ← `integration.registry`<br>integration installation protocol ← `integration.installations`<br>canonical integration installation aggregate ← `integration.installations` | `owner_managed` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_installations.py`<br>`tests/test_integration_installation_api.py`<br>`tests/test_integration_manifest_deployment_gate.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.installations` | immutable integration configuration revisions | `authoritative_record` | deployed connector manifest ← `integration.registry`<br>approved integration secret references ← `secrets.reference_store`<br>integration installation protocol ← `integration.installations`<br>canonical integration installation aggregate ← `integration.installations` | `owner_managed` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_installations.py`<br>`tests/test_integration_installation_api.py`<br>`tests/test_integration_manifest_deployment_gate.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.installations` | integration capability grants and bindings | `authoritative_record` | deployed connector manifest ← `integration.registry`<br>integration installation protocol ← `integration.installations`<br>canonical integration installation aggregate ← `integration.installations` | `owner_managed` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_installations.py`<br>`tests/test_integration_installation_api.py`<br>`tests/test_integration_manifest_deployment_gate.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.runtime` | version-pinned connector runner selection | `resolver` | deployed connector runtime definition ← `integration.registry`<br>enabled version-pinned capability binding ← `integration.installations`<br>bounded integration secret materialization ← `secrets.reference_store` | `read_only` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_manifest_registry.py`<br>`tests/test_integration_installations.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.runtime` | connector operation envelope construction | `policy` | deployed connector runtime definition ← `integration.registry`<br>enabled version-pinned capability binding ← `integration.installations`<br>bounded integration secret materialization ← `secrets.reference_store` | `read_only` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_manifest_registry.py`<br>`tests/test_integration_installations.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.runtime` | bounded secret materialization for connector execution | `policy` | deployed connector runtime definition ← `integration.registry`<br>enabled version-pinned capability binding ← `integration.installations`<br>bounded integration secret materialization ← `secrets.reference_store` | `read_only` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_manifest_registry.py`<br>`tests/test_integration_installations.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.delivery` | integration event subscription projection | `projection_writer` | canonical domain event envelope ← `events.store`<br>enabled outbound capability binding ← `integration.installations`<br>integration delivery protocol ← `integration.delivery` | `owner_managed` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_delivery.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.delivery` | deduplicated integration delivery lifecycle | `command_writer` | canonical domain event envelope ← `events.store`<br>enabled outbound capability binding ← `integration.installations`<br>connector delivery receipt ← `integration.runtime`<br>integration delivery protocol ← `integration.delivery` | `owner_managed` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_delivery.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.delivery` | outbound capability delivery evidence | `authoritative_record` | canonical domain event envelope ← `events.store`<br>enabled outbound capability binding ← `integration.installations`<br>connector delivery receipt ← `integration.runtime` | `owner_managed` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_delivery.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.inbox` | verified provider event receipt identity | `observation_collector` | verified external provider event ← `external:integration_provider`<br>enabled inbound capability binding ← `integration.installations`<br>integration inbox protocol ← `integration.inbox` | `owner_managed` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_installation_api.py`<br>`tests/test_integration_whatsapp_capability.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.inbox` | integration inbox deduplication lifecycle | `authoritative_record` | verified external provider event ← `external:integration_provider`<br>enabled inbound capability binding ← `integration.installations`<br>integration inbox protocol ← `integration.inbox` | `owner_managed` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_installation_api.py`<br>`tests/test_integration_whatsapp_capability.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.inbox` | inbound consequence processing evidence | `command_writer` | canonical domain consequence result ← `integration.runtime`<br>integration inbox protocol ← `integration.inbox` | `owner_managed` | `complete` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_installation_api.py`<br>`tests/test_integration_whatsapp_capability.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.jobs` | integration targets | `authoritative_record` | deployed capability contract ← `integration.registry`<br>enabled integration capability binding ← `integration.installations`<br>integration job lifecycle protocol ← `integration.jobs`<br>scheduler-owned cadence ← `scheduler.registry` | `owner_managed` | `cutover_ready` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/runbooks/CRM_TICKET_CAPABILITY_CUTOVER.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_capability_sync.py`<br>`tests/test_crm_ticket_capability_cutover.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.jobs` | integration jobs | `authoritative_record` | deployed capability contract ← `integration.registry`<br>enabled integration capability binding ← `integration.installations`<br>integration job lifecycle protocol ← `integration.jobs`<br>scheduler-owned cadence ← `scheduler.registry` | `owner_managed` | `cutover_ready` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/runbooks/CRM_TICKET_CAPABILITY_CUTOVER.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_capability_sync.py`<br>`tests/test_crm_ticket_capability_cutover.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.jobs` | integration runs | `authoritative_record` | deployed capability contract ← `integration.registry`<br>enabled integration capability binding ← `integration.installations`<br>integration job lifecycle protocol ← `integration.jobs`<br>scheduler-owned cadence ← `scheduler.registry` | `owner_managed` | `cutover_ready` | platform integrations | `docs/designs/INTEGRATION_PLATFORM_SOT.md`<br>`docs/runbooks/CRM_TICKET_CAPABILITY_CUTOVER.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_integration_capability_sync.py`<br>`tests/test_crm_ticket_capability_cutover.py`<br>`tests/architecture/test_integration_platform_boundary.py` |
| `integration.dotmac_erp_payables_adapter` | Dotmac ERP purchase-invoice payload mapping | `projection_writer` | canonical vendor purchase-invoice records ← `operations.vendor_purchase_invoice_records`<br>ERP purchase-invoice origination response ← `external:dotmac_erp`<br>ERP purchase-invoice flow controls ← `control.settings_spec` | `owner_managed` | `cut_over` | vendor finance integrations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_vendor_payment_visibility.py`<br>`tests/test_dotmac_erp_outbox.py` |
| `integration.dotmac_erp_payables_adapter` | Dotmac ERP attachment delivery | `projection_writer` | canonical vendor purchase-invoice records ← `operations.vendor_purchase_invoice_records`<br>ERP purchase-invoice attachment response ← `external:dotmac_erp`<br>ERP purchase-invoice flow controls ← `control.settings_spec` | `owner_managed` | `cut_over` | vendor finance integrations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_vendor_payment_visibility.py`<br>`tests/test_dotmac_erp_outbox.py` |
| `integration.dotmac_erp_payables_adapter` | timestamped Dotmac ERP payables-status observation | `reconciler` | canonical vendor purchase-invoice records ← `operations.vendor_purchase_invoice_records`<br>ERP accounts-payable status observation ← `external:dotmac_erp`<br>ERP purchase-invoice flow controls ← `control.settings_spec` | `owner_managed` | `cut_over` | vendor finance integrations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_vendor_payment_visibility.py`<br>`tests/test_dotmac_erp_outbox.py` |
| `integration.dotmac_erp_material_support_adapter` | Sub-to-Dotmac-ERP material-support payload mapping | `resolver` | approved canonical material dependency ← `operations.material_dependencies`<br>ERP material-support transport contract ← `control.settings_spec` | `coordinator_managed` | `cutover_ready` | field operations integrations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_dotmac_erp_material_sync.py`<br>`tests/test_field_material_requests.py` |
| `integration.dotmac_erp_material_support_adapter` | provider-specific stable idempotency key | `policy` | approved canonical material dependency ← `operations.material_dependencies`<br>ERP material-support transport contract ← `control.settings_spec` | `coordinator_managed` | `cutover_ready` | field operations integrations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_dotmac_erp_material_sync.py`<br>`tests/test_field_material_requests.py` |
| `integration.dotmac_erp_material_support_adapter` | Dotmac ERP material-outcome observation and reconciliation | `application_coordinator` | canonical material dependency projection target ← `operations.material_dependencies`<br>ERP material-support outcome response ← `external:dotmac_erp`<br>ERP material-support transport contract ← `control.settings_spec` | `coordinator_managed` | `cutover_ready` | field operations integrations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_dotmac_erp_material_sync.py`<br>`tests/test_field_material_requests.py` |
| `ui.referral_list_projection` | admin referral filter and stable sort semantics | `resolver` | canonical referral program state ← `referrals.program`<br>normalized referral list query ← `ui.list_contracts`<br>UI projection vocabulary ← `ui.projection_contracts` | `read_only` | `complete` | subscriber growth | `docs/designs/LIST_QUERY_MIGRATION.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_referrals_list.py`<br>`tests/architecture/test_template_projection_boundary.py` |
| `ui.referral_list_projection` | admin referral row and page projection | `resolver` | canonical referral program state ← `referrals.program`<br>normalized referral list query ← `ui.list_contracts`<br>UI projection vocabulary ← `ui.projection_contracts` | `read_only` | `complete` | subscriber growth | `docs/designs/LIST_QUERY_MIGRATION.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_referrals_list.py`<br>`tests/architecture/test_template_projection_boundary.py` |
| `ui.referral_list_projection` | admin referral KPI values and exact cohort links | `resolver` | canonical referral program state ← `referrals.program`<br>normalized referral list query ← `ui.list_contracts`<br>UI projection vocabulary ← `ui.projection_contracts` | `read_only` | `complete` | subscriber growth | `docs/designs/LIST_QUERY_MIGRATION.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_referrals_list.py`<br>`tests/architecture/test_template_projection_boundary.py` |
| `ui.referral_list_projection` | admin referral list canonical URL | `resolver` | canonical referral program state ← `referrals.program`<br>normalized referral list query ← `ui.list_contracts`<br>UI projection vocabulary ← `ui.projection_contracts` | `read_only` | `complete` | subscriber growth | `docs/designs/LIST_QUERY_MIGRATION.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_referrals_list.py`<br>`tests/architecture/test_template_projection_boundary.py` |
| `ui.support_ticket_list_projection` | admin support-ticket searchable fields | `resolver` | typed support Ticket list query ← `ui.support_ticket_list_projection`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration` | `read_only` | `complete` | support product UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_list_ui_contract.py`<br>`tests/playwright/e2e/test_customer_portal.py` |
| `ui.support_ticket_list_projection` | admin support-ticket filter semantics | `resolver` | typed support Ticket list query ← `ui.support_ticket_list_projection`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration` | `read_only` | `complete` | support product UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_list_ui_contract.py`<br>`tests/playwright/e2e/test_customer_portal.py` |
| `ui.support_ticket_list_projection` | admin support-ticket stable sort semantics | `resolver` | typed support Ticket list query ← `ui.support_ticket_list_projection`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration` | `read_only` | `complete` | support product UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_list_ui_contract.py`<br>`tests/playwright/e2e/test_customer_portal.py` |
| `ui.support_ticket_list_projection` | admin support-ticket page and status-summary projection | `resolver` | typed support Ticket list query ← `ui.support_ticket_list_projection`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration` | `read_only` | `complete` | support product UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_list_ui_contract.py`<br>`tests/playwright/e2e/test_customer_portal.py` |
| `ui.support_ticket_list_projection` | admin support-ticket export scope | `resolver` | typed support Ticket list query ← `ui.support_ticket_list_projection`<br>canonical ticket lifecycle state ← `support.ticket_lifecycle`<br>ticket configuration ← `support.ticket_configuration` | `read_only` | `complete` | support product UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_list_ui_contract.py`<br>`tests/playwright/e2e/test_customer_portal.py` |
| `ui.work_order_list_projection` | admin work-order searchable fields | `resolver` | canonical work-order list facts ← `operations.work_orders`<br>shared list contract ← `ui.list_contracts` | `read_only` | `complete` | field operations UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_dispatch_work_orders_contracts.py`<br>`tests/test_dispatch_work_orders_csrf.py`<br>`tests/test_work_order_views.py` |
| `ui.work_order_list_projection` | admin work-order status and native project-task filter semantics | `policy` | canonical work-order list facts ← `operations.work_orders`<br>canonical project-task scope ← `operations.project_lifecycle`<br>shared list contract ← `ui.list_contracts` | `read_only` | `complete` | field operations UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_dispatch_work_orders_contracts.py`<br>`tests/test_dispatch_work_orders_csrf.py`<br>`tests/test_work_order_views.py` |
| `ui.work_order_list_projection` | admin work-order stable sort semantics | `policy` | canonical work-order list facts ← `operations.work_orders`<br>shared list contract ← `ui.list_contracts` | `read_only` | `complete` | field operations UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_dispatch_work_orders_contracts.py`<br>`tests/test_dispatch_work_orders_csrf.py`<br>`tests/test_work_order_views.py` |
| `ui.work_order_list_projection` | admin work-order list pagination normalization | `policy` | shared list contract ← `ui.list_contracts` | `read_only` | `complete` | field operations UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_dispatch_work_orders_contracts.py`<br>`tests/test_dispatch_work_orders_csrf.py`<br>`tests/test_work_order_views.py` |
| `ui.work_order_list_projection` | admin work-order global KPI and exact-cohort link projection | `resolver` | canonical work-order list facts ← `operations.work_orders`<br>UI projection vocabulary ← `ui.projection_contracts` | `read_only` | `complete` | field operations UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_dispatch_work_orders_contracts.py`<br>`tests/test_dispatch_work_orders_csrf.py`<br>`tests/test_work_order_views.py` |
| `ui.work_order_list_projection` | admin task-originated work-order creation prefill | `resolver` | canonical project-task scope ← `operations.project_lifecycle`<br>canonical subscriber scope ← `customer.accounts`<br>work-order creation protocol ← `operations.work_order_commands` | `read_only` | `complete` | field operations UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_dispatch_work_orders_contracts.py`<br>`tests/test_dispatch_work_orders_csrf.py`<br>`tests/test_work_order_views.py` |
| `ui.project_list_projection` | admin project searchable fields | `resolver` | canonical project list facts ← `operations.project_lifecycle`<br>shared list contract ← `ui.list_contracts` | `read_only` | `complete` | service delivery UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_web_projects_service.py`<br>`tests/test_web_admin_projects_render.py`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_projects_api.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `ui.project_list_projection` | admin project filter and stable sort semantics | `policy` | canonical project list facts ← `operations.project_lifecycle`<br>shared list contract ← `ui.list_contracts` | `read_only` | `complete` | service delivery UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_web_projects_service.py`<br>`tests/test_web_admin_projects_render.py`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_projects_api.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `ui.project_list_projection` | admin project list pagination normalization | `policy` | shared list contract ← `ui.list_contracts` | `read_only` | `complete` | service delivery UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_web_projects_service.py`<br>`tests/test_web_admin_projects_render.py`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_projects_api.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `ui.project_list_projection` | admin project-task list field-work action projection | `policy` | canonical project detail facts ← `operations.project_lifecycle`<br>native linked field-work facts ← `operations.work_orders`<br>work-order creation protocol ← `operations.work_order_commands` | `read_only` | `complete` | service delivery UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_web_projects_service.py`<br>`tests/test_web_admin_projects_render.py`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_projects_api.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `ui.project_list_projection` | admin project and task detail field-work composition | `resolver` | canonical project detail facts ← `operations.project_lifecycle`<br>native linked field-work facts ← `operations.work_orders` | `read_only` | `complete` | service delivery UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_web_projects_service.py`<br>`tests/test_web_admin_projects_render.py`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_projects_api.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `ui.project_list_projection` | admin project-task work-order creation action projection | `policy` | canonical project detail facts ← `operations.project_lifecycle`<br>work-order creation protocol ← `operations.work_order_commands` | `read_only` | `complete` | service delivery UI | `docs/designs/PROJECTS_SOT_COMPLETION.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_web_projects_service.py`<br>`tests/test_web_admin_projects_render.py`<br>`tests/test_web_dispatch_work_orders.py`<br>`tests/test_projects_api.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `ui.vendor_supply_projection` | vendor project supply workspace projection | `resolver` | canonical vendor project lifecycle facts ← `operations.vendor_project_lifecycle`<br>canonical vendor material release decisions ← `operations.vendor_material_release`<br>canonical vendor advance decisions ← `operations.vendor_advances`<br>vendor supply request capabilities ← `auth.permission_gate`<br>canonical vendor supply status presentation ← `ui.status_presentation` | `read_only` | `complete` | vendor operations UI | `docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_vendor_supply_ui.py`<br>`tests/architecture/test_vendor_supply_ui_boundary.py` |
| `ui.vendor_supply_projection` | staff vendor supply review queues and impact previews | `resolver` | canonical vendor material release decisions ← `operations.vendor_material_release`<br>canonical vendor advance decisions ← `operations.vendor_advances`<br>canonical vendor project records ← `operations.vendor_project_records`<br>staff vendor supply review capabilities ← `auth.permission_gate` | `read_only` | `complete` | vendor operations UI | `docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_vendor_supply_ui.py`<br>`tests/architecture/test_vendor_supply_ui_boundary.py` |
| `ui.vendor_supply_projection` | latest active vendor supply record selection | `resolver` | canonical vendor material release decisions ← `operations.vendor_material_release`<br>canonical vendor advance decisions ← `operations.vendor_advances` | `read_only` | `complete` | vendor operations UI | `docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_vendor_supply_ui.py`<br>`tests/architecture/test_vendor_supply_ui_boundary.py` |
| `ui.vendor_supply_projection` | material provider issue observation presentation | `resolver` | material provider issue observation ← `operations.vendor_material_release` | `read_only` | `complete` | vendor operations UI | `docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_vendor_supply_ui.py`<br>`tests/architecture/test_vendor_supply_ui_boundary.py` |
| `ui.vendor_supply_projection` | advance payables observation presentation | `resolver` | advance payables settlement observation ← `operations.vendor_advances` | `read_only` | `complete` | vendor operations UI | `docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_vendor_supply_ui.py`<br>`tests/architecture/test_vendor_supply_ui_boundary.py` |
| `ui.vendor_delivery_portfolio_projection` | admin vendor operational portfolio composition | `resolver` | authorized vendor portfolio scope ← `auth.permission_gate`<br>canonical vendor project lifecycle facts ← `operations.vendor_project_lifecycle`<br>canonical project vendor-delivery composition ← `ui.project_vendor_delivery_projection`<br>canonical latest vendor supply projection ← `ui.vendor_supply_projection`<br>canonical vendor status presentation ← `ui.status_presentation` | `read_only` | `complete` | vendor operations UI | `docs/designs/VENDOR_DELIVERY_PORTFOLIO_UI.md`<br>`docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_vendor_delivery_portfolio.py`<br>`tests/architecture/test_vendor_delivery_portfolio_boundary.py` |
| `ui.vendor_delivery_portfolio_projection` | admin vendor project portfolio filtering and pagination | `resolver` | authorized vendor portfolio scope ← `auth.permission_gate`<br>canonical vendor project lifecycle facts ← `operations.vendor_project_lifecycle`<br>vendor portfolio query contract ← `ui.vendor_delivery_portfolio_projection` | `read_only` | `complete` | vendor operations UI | `docs/designs/VENDOR_DELIVERY_PORTFOLIO_UI.md`<br>`docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_vendor_delivery_portfolio.py`<br>`tests/architecture/test_vendor_delivery_portfolio_boundary.py` |
| `ui.vendor_delivery_portfolio_projection` | admin vendor portfolio KPI and cohort parity | `resolver` | canonical vendor project lifecycle facts ← `operations.vendor_project_lifecycle`<br>canonical vendor status presentation ← `ui.status_presentation`<br>vendor portfolio query contract ← `ui.vendor_delivery_portfolio_projection` | `read_only` | `complete` | vendor operations UI | `docs/designs/VENDOR_DELIVERY_PORTFOLIO_UI.md`<br>`docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_vendor_delivery_portfolio.py`<br>`tests/architecture/test_vendor_delivery_portfolio_boundary.py` |
| `ui.vendor_delivery_portfolio_projection` | admin vendor portfolio field visibility | `policy` | authorized vendor portfolio scope ← `auth.permission_gate`<br>canonical project vendor-delivery composition ← `ui.project_vendor_delivery_projection`<br>canonical latest vendor supply projection ← `ui.vendor_supply_projection` | `read_only` | `complete` | vendor operations UI | `docs/designs/VENDOR_DELIVERY_PORTFOLIO_UI.md`<br>`docs/designs/VENDOR_PROJECT_REVIEW_UI.md`<br>`docs/designs/VENDOR_SUPPLY_UI.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`tests/test_vendor_delivery_portfolio.py`<br>`tests/architecture/test_vendor_delivery_portfolio_boundary.py` |
| `ui.project_vendor_delivery_projection` | admin project vendor-delivery composition | `resolver` | canonical installation-project lifecycle facts ← `operations.vendor_project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records`<br>canonical vendor purchase-invoice projection ← `operations.vendor_purchase_invoices`<br>timestamped ERP accounts-payable observation ← `integration.dotmac_erp_payables_adapter`<br>canonical vendor status presentation ← `ui.status_presentation`<br>project-detail read capabilities ← `auth.permission_gate` | `read_only` | `native` | service delivery UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_project_vendor_delivery_projection.py`<br>`tests/test_web_admin_projects_render.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `ui.project_vendor_delivery_projection` | admin project vendor-delivery current-record selection | `resolver` | canonical vendor project records ← `operations.vendor_project_records`<br>canonical vendor purchase-invoice projection ← `operations.vendor_purchase_invoices` | `read_only` | `native` | service delivery UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_project_vendor_delivery_projection.py`<br>`tests/test_web_admin_projects_render.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `ui.project_vendor_delivery_projection` | admin project vendor-delivery field visibility | `policy` | project-detail read capabilities ← `auth.permission_gate`<br>canonical installation-project lifecycle facts ← `operations.vendor_project_lifecycle`<br>canonical vendor project records ← `operations.vendor_project_records`<br>canonical vendor purchase-invoice projection ← `operations.vendor_purchase_invoices` | `read_only` | `native` | service delivery UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_project_vendor_delivery_projection.py`<br>`tests/test_web_admin_projects_render.py`<br>`tests/architecture/test_projects_sot_boundary.py` |
| `ui.support_ticket_bulk_action_projection` | admin support-ticket bulk action visibility | `policy` | bulk interaction contract ← `ui.bulk_action_contracts`<br>support Ticket list projection ← `ui.support_ticket_list_projection`<br>support Ticket bulk preview ← `support.ticket_bulk_commands` | `read_only` | `complete` | support product UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_bulk_actions.py`<br>`tests/test_support_ticket_list_ui_contract.py` |
| `ui.support_ticket_bulk_action_projection` | admin support-ticket page-selection presentation | `policy` | bulk interaction contract ← `ui.bulk_action_contracts`<br>support Ticket list projection ← `ui.support_ticket_list_projection`<br>support Ticket bulk preview ← `support.ticket_bulk_commands` | `read_only` | `complete` | support product UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_bulk_actions.py`<br>`tests/test_support_ticket_list_ui_contract.py` |
| `ui.support_ticket_bulk_action_projection` | admin support-ticket row eligibility presentation | `policy` | bulk interaction contract ← `ui.bulk_action_contracts`<br>support Ticket list projection ← `ui.support_ticket_list_projection`<br>support Ticket bulk preview ← `support.ticket_bulk_commands` | `read_only` | `complete` | support product UI | `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/designs/SUPPORT_UX_POLISH_AUDIT.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_support_ticket_bulk_actions.py`<br>`tests/test_support_ticket_list_ui_contract.py` |
| `ui.invoice_batch_action_projection` | admin invoice batch exact-scope preview | `policy` | canonical invoice batch dry-run facts ← `financial.billing_automation`<br>authorized billing staff scope ← `auth.permission_gate` | `read_only` | `complete` | billing operations UI | `docs/designs/INVOICE_BATCH_AND_REMINDER_SAFE_ACTIONS.md`<br>`docs/FRONTEND_SPEC.md`<br>`tests/test_billing_invoice_batch_web.py`<br>`tests/test_billing_invoice_templates.py`<br>`tests/architecture/test_action_form_ownership.py` |
| `ui.invoice_batch_action_projection` | admin invoice batch fingerprint and confirmation projection | `policy` | canonical invoice batch dry-run facts ← `financial.billing_automation`<br>authorized billing staff scope ← `auth.permission_gate` | `read_only` | `complete` | billing operations UI | `docs/designs/INVOICE_BATCH_AND_REMINDER_SAFE_ACTIONS.md`<br>`docs/FRONTEND_SPEC.md`<br>`tests/test_billing_invoice_batch_web.py`<br>`tests/test_billing_invoice_templates.py`<br>`tests/architecture/test_action_form_ownership.py` |
| `ui.invoice_batch_action_projection` | admin billing-run retry eligibility presentation | `policy` | canonical invoice batch dry-run facts ← `financial.billing_automation`<br>authorized billing staff scope ← `auth.permission_gate` | `read_only` | `complete` | billing operations UI | `docs/designs/INVOICE_BATCH_AND_REMINDER_SAFE_ACTIONS.md`<br>`docs/FRONTEND_SPEC.md`<br>`tests/test_billing_invoice_batch_web.py`<br>`tests/test_billing_invoice_templates.py`<br>`tests/architecture/test_action_form_ownership.py` |
| `ui.service_extension_detail_projection` | admin service-extension detail projection | `resolver` | canonical service-extension lifecycle facts ← `financial.service_extensions`<br>canonical service-extension activity evidence ← `observability.audit_log`<br>canonical staff display identity ← `auth.staff_provisioning`<br>service-extension permission result ← `auth.permission_gate`<br>application display-timezone policy ← `ui.display_formatting`<br>service-extension presentation policy ← `ui.service_extension_detail_projection` | `read_only` | `complete` | billing operations UI | `docs/designs/SERVICE_EXTENSION_LIFECYCLE_SOT.md`<br>`docs/FRONTEND_SPEC.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_billing_service_extensions.py`<br>`tests/architecture/test_service_extension_sot_boundary.py` |
| `ui.service_extension_detail_projection` | exact service-extension activity presentation | `resolver` | canonical service-extension lifecycle facts ← `financial.service_extensions`<br>canonical service-extension activity evidence ← `observability.audit_log`<br>canonical staff display identity ← `auth.staff_provisioning`<br>application display-timezone policy ← `ui.display_formatting` | `read_only` | `complete` | billing operations UI | `docs/designs/SERVICE_EXTENSION_LIFECYCLE_SOT.md`<br>`docs/FRONTEND_SPEC.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_billing_service_extensions.py`<br>`tests/architecture/test_service_extension_sot_boundary.py` |
| `ui.service_extension_detail_projection` | service-extension status and action presentation | `policy` | canonical service-extension lifecycle facts ← `financial.service_extensions`<br>service-extension permission result ← `auth.permission_gate`<br>service-extension presentation policy ← `ui.service_extension_detail_projection` | `read_only` | `complete` | billing operations UI | `docs/designs/SERVICE_EXTENSION_LIFECYCLE_SOT.md`<br>`docs/FRONTEND_SPEC.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_web_billing_service_extensions.py`<br>`tests/architecture/test_service_extension_sot_boundary.py` |
| `ui.projection_contracts` | UI value availability and freshness contract | `policy` | UI projection contract vocabulary ← `ui.projection_contracts` | `not_applicable` | `complete` | platform UI | `docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_ui_contracts.py`<br>`tests/architecture/test_template_projection_boundary.py` |
| `ui.projection_contracts` | UI KPI exact-cohort contract | `policy` | UI projection contract vocabulary ← `ui.projection_contracts` | `not_applicable` | `complete` | platform UI | `docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_ui_contracts.py`<br>`tests/architecture/test_template_projection_boundary.py` |
| `ui.projection_contracts` | UI action eligibility and confirmation contract | `policy` | UI projection contract vocabulary ← `ui.projection_contracts` | `not_applicable` | `complete` | platform UI | `docs/designs/UI_PROJECTION_CONTRACTS.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_ui_contracts.py`<br>`tests/architecture/test_template_projection_boundary.py` |
| `ui.operational_evidence_projection` | question-driven operational evidence projection | `resolver` | bounded collector and task observations ← `observability.recording`<br>scheduler expectation ← `scheduler.registry`<br>integration capability binding facts ← `integration.installations`<br>native quote cutover controls ← `control.feature_registry` | `read_only` | `complete` | platform operations UI | `docs/designs/OPERATIONAL_EVIDENCE_AND_RETRY.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/runbooks/PAYSTACK_AUTOMATIC_POSTING.md`<br>`tests/test_operational_evidence_followup.py`<br>`tests/test_web_network_noc.py`<br>`tests/test_integrations_observability.py` |
| `ui.operational_evidence_projection` | operational retry and next-action projection | `resolver` | bounded collector and task observations ← `observability.recording`<br>scheduler expectation ← `scheduler.registry`<br>integration capability binding facts ← `integration.installations`<br>native quote cutover controls ← `control.feature_registry` | `read_only` | `complete` | platform operations UI | `docs/designs/OPERATIONAL_EVIDENCE_AND_RETRY.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/runbooks/PAYSTACK_AUTOMATIC_POSTING.md`<br>`tests/test_operational_evidence_followup.py`<br>`tests/test_web_network_noc.py`<br>`tests/test_integrations_observability.py` |
| `ui.operational_evidence_projection` | payment automation operational evidence projection | `resolver` | bounded collector and task observations ← `observability.recording`<br>scheduler expectation ← `scheduler.registry`<br>integration capability binding facts ← `integration.installations`<br>canonical payment-provider observations ← `financial.payment_provider_events`<br>canonical top-up reconciliation backlog ← `financial.payment_reconciliation` | `read_only` | `complete` | platform operations UI | `docs/designs/OPERATIONAL_EVIDENCE_AND_RETRY.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/runbooks/PAYSTACK_AUTOMATIC_POSTING.md`<br>`tests/test_operational_evidence_followup.py`<br>`tests/test_web_network_noc.py`<br>`tests/test_integrations_observability.py` |
| `ui.billing_account_workspace_projection` | admin billing-account first-viewport projection | `resolver` | canonical billing-account state ← `customer.accounts`<br>canonical billing-mode profile ← `financial.billing_profile`<br>canonical customer financial position ← `customer.financial_position`<br>UI projection vocabulary ← `ui.projection_contracts` | `read_only` | `complete` | finance operations | `docs/designs/BILLING_ACCOUNT_360.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_accounts_list.py`<br>`tests/test_billing_statement_service.py`<br>`tests/architecture/test_template_projection_boundary.py` |
| `ui.billing_account_workspace_projection` | admin account-statement currency summary projection | `resolver` | canonical customer financial events ← `financial.ledger`<br>UI projection vocabulary ← `ui.projection_contracts` | `read_only` | `complete` | finance operations | `docs/designs/BILLING_ACCOUNT_360.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_accounts_list.py`<br>`tests/test_billing_statement_service.py`<br>`tests/architecture/test_template_projection_boundary.py` |
| `ui.billing_account_workspace_projection` | admin account-statement row and source-link projection | `resolver` | canonical customer financial events ← `financial.ledger`<br>canonical financial document identities ← `financial.ledger` | `read_only` | `complete` | finance operations | `docs/designs/BILLING_ACCOUNT_360.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_billing_accounts_list.py`<br>`tests/test_billing_statement_service.py`<br>`tests/architecture/test_template_projection_boundary.py` |
| `ui.portal_account_health_projection` | customer and reseller account-health first-viewport projection | `resolver` | canonical account state ← `customer.accounts`<br>canonical billing profile ← `financial.billing_profile`<br>canonical customer financial position ← `customer.financial_position`<br>canonical service-health rows ← `ui.portal_account_health_projection`<br>UI projection vocabulary ← `ui.projection_contracts` | `read_only` | `complete` | customer operations | `docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md`<br>`docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_portal_account_health.py`<br>`tests/test_network_sot_services.py`<br>`tests/test_connection_health_ui_contract.py`<br>`mobile/test/models_test.dart`<br>`mobile/test/connection_status_test.dart` |
| `ui.portal_account_health_projection` | subscription-scoped service-health row projection | `resolver` | canonical current subscriptions ← `access.subscription_lifecycle`<br>canonical service access decision ← `customer.service_status`<br>canonical live-session evidence ← `network.radius_sessions`<br>canonical connection and outage diagnosis ← `network.connection_health`<br>canonical pending service change ← `service_intent.subscription_lifecycle`<br>UI status semantics ← `ui.status_presentation` | `read_only` | `complete` | customer operations | `docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md`<br>`docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_portal_account_health.py`<br>`tests/test_network_sot_services.py`<br>`tests/test_connection_health_ui_contract.py`<br>`mobile/test/models_test.dart`<br>`mobile/test/connection_status_test.dart` |
| `ui.portal_account_health_projection` | pending service-change presentation | `resolver` | canonical pending service change ← `service_intent.subscription_lifecycle`<br>canonical current subscriptions ← `access.subscription_lifecycle` | `read_only` | `complete` | customer operations | `docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md`<br>`docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_portal_account_health.py`<br>`tests/test_network_sot_services.py`<br>`tests/test_connection_health_ui_contract.py`<br>`mobile/test/models_test.dart`<br>`mobile/test/connection_status_test.dart` |
| `ui.portal_account_health_projection` | portal financial-position currency-lane projection | `resolver` | canonical billing profile ← `financial.billing_profile`<br>canonical customer financial position ← `customer.financial_position`<br>UI projection vocabulary ← `ui.projection_contracts` | `read_only` | `complete` | customer operations | `docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md`<br>`docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_portal_account_health.py`<br>`tests/test_network_sot_services.py`<br>`tests/test_connection_health_ui_contract.py`<br>`mobile/test/models_test.dart`<br>`mobile/test/connection_status_test.dart` |
| `ui.portal_account_health_projection` | mobile account-health transport projection | `resolver` | canonical account-health projection ← `ui.portal_account_health_projection`<br>UI projection vocabulary ← `ui.projection_contracts` | `read_only` | `complete` | customer operations | `docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md`<br>`docs/designs/CUSTOMER_SELF_SERVICE_LIFECYCLE.md`<br>`docs/UI_INFORMATION_AND_ACTION_STANDARD.md`<br>`docs/SOT_RELATIONSHIP_MAP.md`<br>`tests/test_portal_account_health.py`<br>`tests/test_network_sot_services.py`<br>`tests/test_connection_health_ui_contract.py`<br>`mobile/test/models_test.dart`<br>`mobile/test/connection_status_test.dart` |
| `sales.capture` | provider-neutral Party-first Lead capture command | `application_coordinator` | validated lead-capture contract ← `sales.capture`<br>canonical Party identity state ← `party.registry`<br>canonical Lead lifecycle state ← `sales.lead_lifecycle` | `owner_managed` | `complete` | sales operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_lead_capture_webhook.py`<br>`tests/test_sales_capture_account_conversion.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `sales.capture` | source-interaction idempotency and collision decision | `policy` | validated lead-capture contract ← `sales.capture`<br>immutable captured origin evidence ← `sales.lead_lifecycle` | `owner_managed` | `complete` | sales operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_lead_capture_webhook.py`<br>`tests/test_sales_capture_account_conversion.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `sales.capture` | verified integration receipt to Lead consequence | `application_coordinator` | verified integration receipt ← `integration.inbox`<br>validated lead-capture contract ← `sales.capture`<br>canonical Party identity state ← `party.registry`<br>canonical Lead lifecycle state ← `sales.lead_lifecycle` | `owner_managed` | `complete` | sales operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_lead_capture_webhook.py`<br>`tests/test_sales_capture_account_conversion.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `sales.account_conversion` | exact Lead and Party account conversion | `application_coordinator` | canonical attributed Lead state ← `sales.lead_lifecycle`<br>canonical Party identity state ← `party.registry`<br>reviewed account conversion command ← `sales.account_conversion`<br>canonical customer account state ← `customer.accounts` | `owner_managed` | `complete` | sales operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_capture_account_conversion.py`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `sales.account_conversion` | customer and pending-subscriber role establishment | `application_coordinator` | canonical Party identity state ← `party.registry`<br>canonical customer account state ← `customer.accounts`<br>reviewed account conversion command ← `sales.account_conversion` | `owner_managed` | `complete` | sales operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_capture_account_conversion.py`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `sales.fulfillment` | SalesOrder implementation-scope coordination | `application_coordinator` | canonical SalesOrder implementation contract ← `sales.orders`<br>configured project defaults ← `control.settings_spec`<br>canonical native project state ← `operations.project_lifecycle`<br>canonical installation scope ← `operations.installation_scope` | `owner_managed` | `complete` | sales and service delivery | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_sales_orders_services.py`<br>`tests/test_sales_lifecycle_migration.py`<br>`tests/test_billing_shadow_pipeline.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `sales.fulfillment` | verified implementation release coordination | `application_coordinator` | canonical vendor verification evidence ← `operations.vendor_project_lifecycle`<br>canonical native project state ← `operations.project_lifecycle`<br>canonical sales ServiceOrder state ← `operations.service_order_lifecycle` | `owner_managed` | `complete` | sales and service delivery | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_sales_orders_services.py`<br>`tests/test_sales_lifecycle_migration.py`<br>`tests/test_billing_shadow_pipeline.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `sales.fulfillment` | committed lifecycle output consumption | `command_writer` | canonical vendor verification evidence ← `operations.vendor_project_lifecycle`<br>canonical sales ServiceOrder state ← `operations.service_order_lifecycle`<br>canonical SalesOrder implementation contract ← `sales.orders`<br>receipted owner-output deliveries ← `events.owner_outputs` | `owner_managed` | `complete` | sales and service delivery | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_sales_orders_services.py`<br>`tests/test_sales_lifecycle_migration.py`<br>`tests/test_billing_shadow_pipeline.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `customer.experience_handoff` | implementation-to-customer-experience readiness decision | `command_writer` | canonical sales fulfilment state ← `sales.fulfillment`<br>canonical ServiceOrder completion state ← `operations.service_order_lifecycle`<br>canonical subscription access state ← `access.subscription_lifecycle`<br>customer-experience transition protocol ← `customer.experience_handoff` | `owner_managed` | `complete` | customer experience | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_sales_orders_services.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `customer.experience_handoff` | CX acceptance and needs-attention lifecycle | `command_writer` | canonical CX handoff state ← `customer.experience_handoff`<br>reviewed CX transition command ← `auth.permission_gate`<br>canonical SalesOrder state ← `sales.orders` | `owner_managed` | `complete` | customer experience | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_sales_orders_services.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `customer.experience_handoff` | durable CX actor, time, reason, and event evidence | `authoritative_record` | canonical CX handoff state ← `customer.experience_handoff`<br>reviewed CX transition command ← `auth.permission_gate` | `owner_managed` | `complete` | customer experience | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_CUSTOMER_LIFECYCLE.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_sales_orders_services.py`<br>`tests/architecture/test_service_http_boundary.py` |
| `sales.lifecycle_reconciliation` | sales-to-service projection drift repair orchestration | `reconciler` | canonical SalesOrder delivery state ← `sales.orders`<br>canonical vendor verification evidence ← `operations.vendor_project_lifecycle`<br>canonical ServiceOrder delivery state ← `operations.service_order_lifecycle`<br>canonical CX handoff state ← `customer.experience_handoff` | `owner_managed` | `complete` | sales and service delivery | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`<br>`tests/test_sales_to_service_lifecycle.py`<br>`tests/test_sales_lifecycle_migration.py`<br>`tests/test_sot_relationships.py` |
| `referrals.program` | Party-first Refer & Earn capture policy | `policy` | referral program policy settings ← `control.settings_spec`<br>canonical referrer account state ← `customer.accounts`<br>canonical Party identity and reachability facts ← `party.registry` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_FIRST_REFERRAL_CAPTURE.md`<br>`docs/REFERRAL_ACCOUNT_CONVERSION.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referrals_native.py`<br>`tests/test_admin_referrals_web.py`<br>`tests/test_customer_portal_referrals.py`<br>`tests/architecture/test_referrals_program_boundary.py` |
| `referrals.program` | canonical Referral program record | `authoritative_record` | referral program command evidence ← `referrals.program`<br>referral program policy settings ← `control.settings_spec`<br>canonical referrer account state ← `customer.accounts`<br>canonical Party identity and reachability facts ← `party.registry`<br>canonical attributed Lead state ← `sales.lead_lifecycle` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_FIRST_REFERRAL_CAPTURE.md`<br>`docs/REFERRAL_ACCOUNT_CONVERSION.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referrals_native.py`<br>`tests/test_admin_referrals_web.py`<br>`tests/test_customer_portal_referrals.py`<br>`tests/architecture/test_referrals_program_boundary.py` |
| `referrals.program` | Referral Subscriber attachment record | `authoritative_record` | canonical Referral program record ← `referrals.program`<br>canonical referred account state ← `customer.accounts`<br>canonical Party identity and reachability facts ← `party.registry`<br>canonical attributed Lead state ← `sales.lead_lifecycle` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_FIRST_REFERRAL_CAPTURE.md`<br>`docs/REFERRAL_ACCOUNT_CONVERSION.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referrals_native.py`<br>`tests/test_admin_referrals_web.py`<br>`tests/test_customer_portal_referrals.py`<br>`tests/architecture/test_referrals_program_boundary.py` |
| `referrals.program` | referral qualification and reward policy | `policy` | canonical Referral program record ← `referrals.program`<br>referral program policy settings ← `control.settings_spec`<br>canonical subscriber activation state ← `access.subscription_lifecycle`<br>canonical referral reward credit evidence ← `financial.credit_notes` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_FIRST_REFERRAL_CAPTURE.md`<br>`docs/REFERRAL_ACCOUNT_CONVERSION.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referrals_native.py`<br>`tests/test_admin_referrals_web.py`<br>`tests/test_customer_portal_referrals.py`<br>`tests/architecture/test_referrals_program_boundary.py` |
| `referrals.program` | atomic referral program transition orchestration | `application_coordinator` | referral program command evidence ← `referrals.program`<br>canonical Referral program record ← `referrals.program`<br>referral program policy settings ← `control.settings_spec`<br>canonical referrer account state ← `customer.accounts`<br>canonical referred account state ← `customer.accounts`<br>canonical Party identity and reachability facts ← `party.registry`<br>canonical attributed Lead state ← `sales.lead_lifecycle`<br>canonical subscriber activation state ← `access.subscription_lifecycle`<br>canonical referral reward credit evidence ← `financial.credit_notes` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/PARTY_FIRST_REFERRAL_CAPTURE.md`<br>`docs/REFERRAL_ACCOUNT_CONVERSION.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referrals_native.py`<br>`tests/test_admin_referrals_web.py`<br>`tests/test_customer_portal_referrals.py`<br>`tests/architecture/test_referrals_program_boundary.py` |
| `referrals.account_conversion` | stable Referral Party Lead conversion context validation | `policy` | canonical Referral conversion record ← `referrals.program`<br>canonical referred Party identity ← `party.registry`<br>canonical attributed Lead state ← `sales.lead_lifecycle` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/REFERRAL_ACCOUNT_CONVERSION.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referral_account_conversion.py`<br>`tests/test_referral_self_service_signup.py`<br>`tests/architecture/test_referral_account_conversion_boundary.py` |
| `referrals.account_conversion` | atomic referral account creation and adjudication orchestration | `application_coordinator` | referral account conversion command evidence ← `referrals.account_conversion`<br>canonical Referral conversion record ← `referrals.program`<br>canonical referred Party identity ← `party.registry`<br>canonical attributed Lead state ← `sales.lead_lifecycle`<br>canonical Subscriber account state ← `customer.accounts` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/REFERRAL_ACCOUNT_CONVERSION.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referral_account_conversion.py`<br>`tests/test_referral_self_service_signup.py`<br>`tests/architecture/test_referral_account_conversion_boundary.py` |
| `referrals.account_conversion` | public referral signup capability purpose claims and lifetime | `policy` | canonical Referral conversion record ← `referrals.program`<br>referral signup capability policy settings ← `control.settings_spec`<br>verified public signup capability envelope ← `auth.token_signing` | `coordinator_managed` | `complete` | customer operations | `docs/SOT_RELATIONSHIP_MAP.md`<br>`docs/REFERRAL_ACCOUNT_CONVERSION.md`<br>`docs/adr/0002-owner-command-transaction-boundary.md`<br>`docs/designs/SOT_CODING_STANDARDS_REFACTOR.md`<br>`tests/test_referral_account_conversion.py`<br>`tests/test_referral_self_service_signup.py`<br>`tests/architecture/test_referral_account_conversion_boundary.py` |
<!-- END GENERATED SOT MANIFEST -->


## Party Identity, Roles, and Relationships

The complete approved contract is `docs/PARTY_ROLE_RELATIONSHIP_SOT.md`.
The read-only cleanup contract is `docs/PARTY_IDENTITY_CLEANUP_AUDIT.md`.

`party.registry` is the one native owner for this coherent identity boundary:

1. person/organization identity, data classification, quarantine, merge policy,
   and external-reference provenance;
2. concurrent role lifecycle and the controlled distinction between reseller,
   vendor, partner, customer/subscriber, staff, and agent;
3. directional descriptive relationships between parties, which never grant
   authorization;
4. a person's explicit organization context and bounded access scope, with
   authorization still resolved through `auth.subscriber_assignments` and
   `auth.permission_gate`; and
5. normalized reachability, provider/account scope, immutable social subject
   identity, verification, and consent evidence.

Rule: one real-world person or organization has one canonical Party and may
hold several independent roles. A reseller is a specific commercial channel
role; partner is an explicitly typed collaboration agreement and is not a
reseller alias or permission shortcut. Subscriber, reseller, vendor, staff,
contact, and login records are domain profiles, relationships, memberships, or
principals linked to Party—not separate identities. CRM identifiers are import
provenance only and CRM has no runtime party/lifecycle authority.

Migrations 349 through 355 are additive foundations. Migration 350 gives
Subscriber a nullable, evidence-bound canonical Party link owned by
`party.registry`; one
Party may own several accounts, and an existing link cannot be repointed by the
binding command. The link assigns no role or permission and no row is
backfilled. Existing subscriber, organization, reseller, vendor, FieldVendor,
Team Inbox, and authentication reads remain unchanged until their individual
backfills, parity gates, cutovers, and compatibility-path retirements. Callers
must not infer that the presence of the new tables or link means a domain has
cut over.

`party.identity_audit` is a read-only resolver over native Sub facts. It owns
subscriber cleanup cohort classification, duplicate-candidate evidence groups,
and the private UUID-only worklist contract. It never writes a source model,
calls CRM, or treats any evidence level as permission to merge. Applying a
quarantine, Party backfill, merge, or repoint remains a separate reviewed slice.
Account-level billing blocks remain access-enforcement facts: they do not demote
an active subscription or remove the subscriber lifecycle cohort. The audit
observes subscription lifecycle and account status but owns neither.

`party.identity_adjudication` owns the reviewed decision contract and PII-free
Party backfill dry-run plan. Every decision is bound to the current audit digest
and subscriber-row fingerprint. Medium/high duplicate groups must be resolved
completely before any member enters a plan; multiple accounts share one planned
Party only through an explicit common Party UUID. The planner has no database
writer or apply mode, never infers Person versus Organization, and never turns
duplicate evidence into automatic merge authority. `party.registry` remains the
record writer.

`party.identity_backfill_executor` owns the separately approved execution gate
and PII-free receipt, while delegating predetermined Party creation and binding
to `party.registry`. It requires the exact decision and plan file hashes, audit
and plan digests, an expiring approval with exact count limits, typed digest
confirmation, and a PostgreSQL `SERIALIZABLE, READ WRITE` transaction. Selected
Subscriber rows are locked and any stale fact, UUID collision, partial state,
repoint, or receipt drift fails closed. The durable receipt manifest makes an
exact retry verifiable and preserves later compensation evidence. The executor
cannot merge identities, assign roles, copy contacts, or change account,
subscription, billing, access, or authorization state. Migration 351 creates
only the receipt schema; it performs no backfill and authorizes no production
execution.

Migration 352 adds evidence-bound, one-to-one Party links to `Organization`,
`Reseller`, `Vendor`, and `FieldVendor`. `party.registry` is their only binding
writer. Profile binding requires an active/quarantined Organization Party, is
idempotent only for the exact existing target, preserves original evidence,
and refuses repoints. It assigns no role or permission. The native Vendor and
its string-bridged FieldVendor auth projection must bind together to the same
Party; missing, partial, conflicting, or duplicate projections fail closed.

`Organization.account_type`, Reseller/Vendor/FieldVendor `is_active`, and the
FieldVendor string UUID remain compatibility state until their runtime callers
pass a documented parity and cutover gate. They are not converted into Party
roles by migration 352. `party.organization_profile_audit` reports aggregate
binding, role-coverage, and vendor-twin debt without identity values or writes.
The complete migration boundary is
`docs/PARTY_ORGANIZATION_PROFILE_BINDING.md`.

Migration 353 adds reviewed Person Party links to `SystemUser` and
`ResellerUser`, and reviewed canonical `PartyMembership` links to
`ResellerUser`, `OrganizationMembership`, and `FieldVendorUser`.
`party.registry` is the only binding writer. A reseller principal must bind to
one `reseller_admin` membership whose Person and Organization agree with its
reviewed reseller profile. A FieldVendorUser binds to one `vendor_user`
membership; its SystemUser must already identify that same Person and both
vendor profiles must already identify the same Organization.
OrganizationMembership role and Organization must agree with the canonical
membership. The unused native VendorUser is not wired into the new boundary.

Migration 353 does not create or activate a PartyMembership, infer identity
from names/email/legacy UUIDs, assign Party roles, change `is_active`, alter
credentials/tokens/RBAC, or change a login/read path. Compatibility state stays
authoritative until an explicit parity cutover. The read-only
`party.principal_context_audit` reports only aggregate schema, binding,
membership-context, and FieldVendorUser context debt. The complete boundary is
`docs/PARTY_PRINCIPAL_CONTEXT_BINDING.md`.

Migration 354 adds an evidence-bound Person Party link to `SubscriberContact`,
reviewed projection tables for its descriptive relationship and individual
legacy contact fields, and an evidence-bound canonical `PartyContactPoint`
projection on `InboxContactLink`. `party.registry` is the only writer for the
SubscriberContact Person, relationship, and source-field projections.
`communications.team_inbox_routing` remains the only writer for Inbox route
lifecycle. `communications.team_inbox_contact_resolution`, through
`team_inbox_contact_links`, is the only writer for the reviewed Inbox
contact-point projection.

The migration is schema-only. It does not infer a person from an email, phone,
name, social handle, or shared account; create a Party, relationship, or contact
point; copy verification or consent; grant access from a descriptive
relationship; change `SubscriberContact.is_authorized`, notification flags, an
Inbox target/active route, subscription state, billing block, or any current
read path. Social projections require provider, connected-account, and
immutable provider-subject scope. Unsupported `other_social`, `chat_widget`,
and `note` values remain explicit audit debt rather than guessed identities.

`party.contact_inbox_audit` reports only aggregate schema, binding,
relationship, contact-point, verification/consent coverage, and Inbox
projection debt. Its operator runs in a PostgreSQL read-only, repeatable-read
transaction. Backfill, shadow parity, reader cutover, and compatibility-path
retirement remain separate approvals. The complete boundary and cutover gates
are `docs/PARTY_CONTACT_INBOX_PROJECTION.md`.

Migration 355 establishes the additive customer-lifecycle boundary. A Lead can
identify a reviewed Party before any Subscriber account exists, and
`sales.lead_lifecycle` owns its immutable structured origin and later reviewed
account attachment. Native Sub communication campaign UUIDs remain owned by
`communications.campaigns`; Meta/Google and other provider campaign IDs remain
external origin provenance and are never coerced into those UUIDs.

`sales.service`, `sales.orders`, `access.subscription_lifecycle`, and
`support.ticket_lifecycle` retain their domain state. Their links are guarded:
Quote Subscriber must match Lead Party, Sales Order Subscriber must match
Quote, and any Ticket customer links must match its Lead Party. A Lead-only
Ticket remains valid for pre-sales support. Billing blocks and Subscription
status are observed by the PII-free `customer.lifecycle_audit`, never decided
or changed by it. CRM and `dotmac_mkt` have no runtime customer-lifecycle or
person-level attribution authority. The complete boundary and cutover gates
are `docs/PARTY_CUSTOMER_LIFECYCLE.md`.

The signed CRM accepted-customer endpoint is observation-only through
`integration.inbox`. It cannot create accounts or write Subscriber identity,
contact, address, category, profile, Party, account, or lifecycle fields.
Exact retained CRM person, sales-order, or quote provenance may report a
read-only match; names, email addresses, and phone numbers cannot establish or
merge identity. Unmatched and ambiguous observations remain Inbox evidence for
review. This retires the former direct CRM customer writer and create fallback.
The incident command at
`scripts.one_off.restore_crm_placeholder_identity` is read-only by default.
Its separately gated apply mode requires an exact plan digest and named target,
then delegates legacy Subscriber corrections to `customer.name_repairs`.
Party-bound rows fail closed pending the explicit Party name-projection
cutover.

Migration 356 applies that boundary to Refer & Earn. `referrals.program` owns
capture policy, the canonical ReferralCode/Referral and exact-Party
account-attachment records, qualification/reward policy, and typed atomic
program transitions; `referrals.account_conversion` owns the cross-domain
conversion command.
It asks `party.registry` to create quarantined identity/reachability facts and
`sales.lead_lifecycle` to create the Lead and immutable referral origin. New
capture creates no Subscriber and duplicates no contact PII into Referral
metadata. Account attachment requires exact reviewed Party equality; contact
matching cannot qualify or relink a referral. The detailed contract is
`docs/PARTY_FIRST_REFERRAL_CAPTURE.md`.

Every program mutation now enters one manifest-verified owner transaction.
Code issuance locks the Subscriber, capture locks the active ReferralCode, and
transitions lock the Referral before Subscriber or financial state. Reward
issuance delegates monetary evidence to `financial.credit_notes` using the
legacy-compatible referral reference, so existing credit evidence repairs the
Referral link without paying twice. PII-free versioned events are staged with
the transition. Reward notification is a deduplicated consequence resolved by
the canonical notification template/channel policy, never an in-service push.
Program settings, including the share-base URL, resolve only through
`control.settings_spec`.

Referral customer reads/writes are permanently native. The prior referral
read/write controls, CRM referral mutation, mirror write-through, and scheduled
outbound reconciliation are retired. The legacy mirror is read-only historical
compatibility evidence, is not an active SOT owner, and cannot feed native
referral decisions. The signed legacy webhook route and old Celery names are
no-op tombstones that absorb queued traffic without database or network work.

Referral signup and operator account adjudication resolve through typed commands
owned by `referrals.account_conversion`. Its stable context is the canonical
Referral/Party/Lead UUID triple already stored by migration 356, so account
conversion adds no parallel table or migration. The coordinator locks and
revalidates the exact Referral, Party, Lead, and selected Subscriber, asks
`customer.accounts` to prepare a new Subscriber when needed, then delegates
Party binding, Lead attachment, and Referral attachment to transaction-neutral
owner collaborators. Account, bindings, PII-free audit, and versioned events
commit or roll back together. A stale context, different Party/account, or
self-referral is refused; contact values never select identity. The detailed
contract is `docs/REFERRAL_ACCOUNT_CONVERSION.md`.

Public capture carries that context forward as a signed, PII-free capability.
`auth.token_signing` owns configured signing-key/algorithm resolution and the
cryptographic envelope; `referrals.account_conversion` owns purpose, claims,
canonical revalidation, and the lifetime decision. The lifetime resolves only
from the bounded, database-authoritative
`subscriber.referral_signup_context_expiry_minutes` setting; its default and
bounds live only in `control.settings_spec`.
Public signup exposes no lifecycle, reseller, billing, verification, numbering,
or permission controls. It also cannot set marketing consent outside the
communication-eligibility owner. The token proves capture continuity only and
does not verify identity or authorize contact matching.

After account creation, `auth.customer_credential_enrollment` owns the separate
credential handoff. It creates no random or placeholder password. It sends a
typed, non-secret communication intent; `communications.ephemeral_actions`
revalidates the canonical context and mints the 24-hour capability only when
the delivery worker is ready to call the email transport. The rendered bearer
exists only in worker memory and is never projected back into the intent,
Notification, audit, delivery error, or log. The local `UserCredential` is
created only when the recipient chooses a password. Successful redemption and
`Subscriber.email_verified` are one transaction and make the capability
single-use through canonical credential state. They do not verify a Party
contact point, activate or merge the quarantined Party, or change account,
subscription, billing-block, access, consent, role, or permission state. The
detailed security and delivery boundary is
`docs/REFERRAL_CREDENTIAL_ENROLLMENT.md`.

## Financial and Access

1. `financial.ledger` owns the append-only record lifecycle and reversal
   invariant. Domain owners decide why money moves.
2. `financial.payments`, `financial.consolidated_payments`,
   `financial.invoices`, and `financial.credit_notes` own their scoped document
   lifecycles, owner-produced previews, and the ledger postings those
   transitions require. Invoice read models expose payment, credit-note, and
   remaining-receivable amounts as distinct fields.
3. `financial.tax_configuration` owns configurable tax-rate records and their
   active lifecycle. Inclusive, exclusive, or exempt treatment belongs to the
   invoice/credit-note line, not to a second tax-rate vocabulary.
4. `financial.payment_proofs` owns proof review, supplies typed verified
   gross/net/WHT evidence to the tax owner when a direct customer or reseller
   payment carries server-owned WHT facts, and decides that a submitted proof
   requires confirmation. Customer-entered WHT is rejected unless it matches a
   server-issued invoice direct-transfer snapshot, and arbitrary consolidated
   credit fails closed for automatic WHT. `financial.tax_accounting` alone
   constructs the source WHT record and initial timeline as a flush-only
   participant of that proof transaction. The proof owner requests one reviewer
   work item from `communications.staff_notifications`; it does not select
   staff recipients, construct WHT rows, or construct inbox/delivery rows.
5. `financial.tax_accounting` owns tax-report meaning, periods, currency
   separation, issued-output-tax and credit-note adjustment projection, net
   output-tax liability, WHT-receivable projection and lifecycle, its immutable
   official transition timeline, and the bounded tax-fact feeds consumed by
   Dotmac ERP. Issued output tax less issued credit-note tax adjustments is the
   source-document liability; it is not labelled as collected cash. Pending and
   certified WHT remain outstanding receivables; reclaimed and written-off
   records remain visible without inflating the outstanding amount. Dotmac ERP
   exclusively owns TaxCode account mappings, balanced journals, tax
   transactions, and financial statements. Sub has no tax posting or account-
   mapping table. Staff lifecycle changes enter a typed owner command on a
   transaction-free session and commit the locked WHT record, append-only
   timeline, payment sync watermark, audit, and versioned event atomically.
   Reports and the operator queue return typed immutable read models and fail
   closed on malformed period, currency, filter, or pagination evidence. A
   real-PostgreSQL concurrency test proves the record lock admits one
   certification and rejects conflicting certificate evidence without a second
   transition or event.
6. The VAS product is retired. Its database tables are immutable financial
   archives, not live balances or action owners. Revision
   `300_retire_vas_runtime` blocks cutover until wallet liabilities are zero and
   provider workflows are terminal; no route, task, setting, or service may
   resume writes to those tables. The cutover and fallback contract is
   `docs/designs/VAS_RETIREMENT.md`.
7. Customer financial position owns read-side financial summaries, including
   the bounded bulk projection used by cohort monitoring. It exposes invoice
   receivables and prepaid service funding as separate values; it does not net
   them into a generic balance or absorb payment lifecycle or service-access
   state. Prepaid funding delegates to
   `financial.prepaid_funding_reconstruction`: one reviewed opening position at
   the final authority-cutover timestamp plus canonical native events strictly
   after that timestamp. There is no Splynx/legacy fallback or authority toggle.
   The opening-position manifest covers the exact funding cohort and is
   Ed25519 sealed against an OpenBao-owned public trust reference before
   materialization. Its default is blocker-free. An explicitly approved partial
   cutover binds verified rows and every quarantined account/reason under the
   same signature; quarantined accounts receive neither a guessed balance nor a
   money-based access consequence.
   A pre-cutover account without an approved opening balance fails closed; an
   account created after cutover starts at zero and accumulates native events.
   Customer statements and scalar funding previews use that reviewed position
   as their opening event. A native fact crosses that boundary when its
   economic timestamp or its Sub `created_at` is later, so late-entered,
   backdated money is not hidden by the opening position. They never replay the
   archived mirror or older duplicate projections.
   Portal outstanding-balance views consume its collection-blocking
   value; a capped invoice display list never caps or redefines the amount.
   Billing reporting applies the same collectible/non-proforma boundary and
   derives settled value from invoice money, not a status label. The account
   balance KPI is collectible AR in one declared currency, never `min_balance`.
   Bulk callers use the same owner instead of reconstructing another balance
   or looping the single-customer ledger reader. Its customer billing headline
   projection is one complete, explicit-currency cohort: total billed excludes
   draft, void, pro-forma, inactive, and other-currency rows; outstanding and
   overdue apply the same currency and non-pro-forma boundary. The portal route
   and template neither sum invoices nor select a currency.
8. `financial.access_resolution` owns financial suspension/restoration
   eligibility. For prepaid service, both directions compare the customer
   financial position with the single `financial.prepaid_threshold` in the
   configured `billing.prepaid_enforcement_currency`; nominal amounts in
   different currencies are never compared. The existence or size of one
   payment is never itself permission to restore.
9. `financial.prepaid_enforcement` owns the prepaid candidate cohort and the
   warn/suspend/restore plan consumed by both dry-run and execution. It consumes
   the funding decision from `financial.access_resolution`; it does not create
   another balance or threshold rule. Migration first materializes a named,
   timestamped, reviewed funding-cohort opening position through
   `financial.prepaid_funding_reconstruction` (for example, from the Splynx
   cutover position plus proven native events). Splynx exports and bank
   statements may close migration evidence, but their rows and narrations are
   never runtime funding. The enforcement owner still applies billing
   profile validity, configured grace, activation time, windows, shields,
   health, and lifecycle policy, including selection of the candidate cohort.
   Activation does not reset an older low-balance timer. A resolved zero-day
   grace policy is actionable on the first eligible sweep; an explicit nonzero
   account or policy-set grace remains authoritative. Supplied snapshots are
   complete-or-error by default. A partial cutover must cryptographically
   partition the exact funding cohort into materialized and quarantined IDs and
   never fill either set from a different balance source. The broader repair
   cohort may clear stale prepaid timers/locks on non-prepaid or service-less
   accounts without creating a funding baseline. Lock repair remains
   reason-scoped: it resolves only the obsolete prepaid lock, preserves every
   unrelated active lock, and never activates a terminal subscription.
   A reviewed never-paid decision may resolve only an exact hash-bound
   `source_service_without_paid_through_period` cohort: it preserves the source
   opening balance, makes the service due immediately, and is bound into the
   signed artifact. Exact-set equality is not evidence that the reason is true:
   the final source service must also have no charge, discount, correction, or
   other service-linked period transaction. Any such evidence gets a separate
   blocker and cannot consume the never-paid disposition. Account-level payment
   receipts remain in opening funding but do not prove a particular service
   period. The disposition is not a generic blocker override.
   After authority cutover, an affordable reconstructed service-cycle charge
   must have an active `ServiceEntitlement` for the same subscription and
   billing-period start, linked either to a paid `financial.invoices` line or
   an exact customer-position service debit. Missing or amount-mismatched funding
   evidence blocks reconstruction; the reconstruction owner never substitutes
   an undocumented charge.
   `financial.prepaid_service_renewals` owns the scheduled or post-credit-
   application case that was not already executed inside the payment owner's
   settlement transaction. When reviewed funding already exists as a monthly
   period becomes due, it previews against the verified position, posts one
   idempotent service debit, links one active entitlement, and advances the
   exact subscription period. A completed account-credit application invokes
   it before access restoration; a lapsed service starts on the payment day and
   missed inactive periods are not silently back-billed.
   A fully paid positive invoice with an active exact prepaid subscription line
   and complete active payment/credit-note applications is the mutually exclusive
   invoice-funded representation. Status alone is not funding evidence. It remains non-AR,
   but `customer.financial_position` projects its tax-inclusive total as the one
   service-consumption debit. An exact unreversed renewal adjustment plus active
   debit-backed entitlement for the same account, subscription, period, amount,
   and currency takes precedence over a later documentary invoice so reviewed
   evidence reconciliation has zero economic delta.
   Every forward renewal also stages `prepaid_service.renewed` with the exact
   entitlement, debit, period start, and renewed-through boundary in the same
   transaction. Notifications and portal success views consume that outcome;
   they do not derive expiry from a payment receipt. A trigger payment ID is
   correlation only because account credit is pooled and does not assign a
   particular renewal debit to one historical payment.
   Its positive contracted `Subscription.unit_price`, discount, tax precedence,
   currency, and cadence produce one exact renewal charge consumed by both the
   renewal executor and prepaid enforcement. Current catalog amount is not
   authority for an already-contracted prepaid service. Missing contract or
   currency/cadence evidence produces a typed no-action outcome; enforcement
   never guesses a price or suspends the account from incomplete terms.
   The scheduled adapter is permanent and refuses anchors more than two days
   stale; historical cycles require a reviewed hash-bound reconciliation plan.
   Reconstruction-quarantined subscriptions and unexpected account-level
   missing baselines are reported and left untouched without aborting unrelated
   verified renewals. Missing global authority still blocks the complete pass.
10. `financial.billing_reporting` (`app/services/billing/reporting.py`) owns
   every money figure the admin reports and overview render: overview and
   payments/collections summaries, AR aging and outstanding receivables,
   revenue by offer/service type, statements, subscription movement, and the
   canonical bases decided 2026-07-16 — figures labelled "Revenue" use the
   invoice settled-value basis, the payments basis is labelled Collections,
   and recurring revenue uses the MRR-countable basis. Report/web layers
   compose these reads and own presentation only.
11. `financial.prepaid_enforcement` owns the account-scoped warn, suspend, and
   restore decision. Every scheduled pass consumes the live currency-bound
   funding owner, canonical coverage, quarantine, billing profile, shields,
   grace, and the shared time-of-day window. It accepts no alternate funding
   input. Historical readiness rows are deployment evidence, not runtime
   authority. Bank statements may close missing source evidence through normal
   reconciliation, but never become a parallel runtime balance. Live
   enforcement consumes only the reviewed opening balance plus canonical native
   events, and unresolved account evidence produces a typed no-action outcome
   without stopping unrelated accounts.
12. `financial.prepaid_plan_change` owns the immediate prepaid plan-change quote,
   affordability decision, confirmation fingerprint, and idempotent financial
   adjustment. It binds the human preview to a durable change request, locks the
   account and recomputes at write time, then records the exact adjustment or
   credit-note and ledger transaction on that request. Portal, admin, API, and
   change-request application paths do not post their own plan-change debit.
   Debits delegate to `financial.account_adjustments`; credits delegate to
   `financial.credit_notes`. Immediate admin bulk changes are gated until a
   batch contract can preview and confirm every subscription separately;
   next-cycle bulk scheduling produces no immediate financial transaction.
13. `financial.account_adjustments` owns debit eligibility, preview, locked
   confirmation, idempotency, actor audit, exact ledger evidence, and previewed
   append-only reversal. It never issues customer credits and never decides
   service-access state.
14. `financial.addon_purchases` owns customer add-on price, subscription-state,
   and entitlement confirmation. A paid add-on delegates one exact debit to
   `financial.account_adjustments` and stores the structural entitlement-to-
   adjustment link; a free add-on explicitly produces no ledger transaction.
15. Dunning owns postpaid enforcement; prepaid enforcement owns prepaid access.
   Both submit owner-produced previews to `financial.dunning`'s shared
   financial-access consequence confirmation. It locks and rechecks billing
   profile validity, payment-arrangement/proof/extension shields, canonical
   receivables or prepaid funding, and billing enforcement health immediately
   before acting. `access.subscription_lifecycle` is the sole writer of
   enforcement locks and subscription/account access status. It persists the
   derived account status and every child service's desired access state in one
   transaction. The mandatory access-control reconciler invokes that owner,
   compares the exact per-login projection consumed by the RADIUS writer in
   both directions, requests one idempotent projection refresh when needed,
   and reports a degraded outcome until the external rows converge.
16. `financial.payment_arrangements` owns arrangement eligibility, lifecycle,
   installment schedule, payment application, and active-arrangement shield
   state. Dunning consumes the shield; it does not reimplement arrangement
   eligibility, and an arrangement does not rewrite receivables or access.
17. `financial.billing_health` owns monitoring snapshots and anomaly
    classification. Health signals are observations, not balances or direct
    suspension/restoration permission. Its frequent snapshot consumes typed,
    database-aggregated invariant counts from the financial owner; exact
    record-level forensic inspection remains a separate owner query and is not
    used merely to produce a metric count.
18. Scheduled billing, collections, and payment-reconciliation services own DB
   sessions, transaction outcomes, and operational logging for Celery runners.
19. `integration.inbox` owns signature-verified payment receipt identity,
   failure state, and replay authorization. `financial.payment_webhooks`
   normalizes only the stored claimed receipt and owns one atomic billing-
   consequence transaction. Provider event, money, allocation, top-up
   projection, audit/event evidence, and the processed-receipt projection
   commit or roll back together. After a rollback, `integration.inbox` records
   retry or dead-letter evidence in its own separate owner transaction;
   `financial.payment_provider_events` owns idempotent event processing,
   delegates the monetary write to the payment owner, and must resume an
   incomplete event rather than treating receipt identity as proof that money
   was posted. The provider-event owner records the explicit admission source,
   normalized status and monetary evidence, stable processing result, and an
   exact evidence digest. Administrative ingestion is informational only;
   verified webhook and gateway-reconciliation participants require their
   named command scopes. Identity reuse with different evidence fails closed,
   and required currency or invoice-settlement net evidence is never guessed.
   Provider-event processing composes named flush-only payment,
   consolidated-settlement, allocation, refund, reversal, status, and exception
   participants; none may commit or roll back the provider-event transaction.
   Verified invoice cash is staged before its optional allocation consequence,
   which runs only through the owner-command savepoint API so failure produces
   reconciliation evidence without leaving partial allocation writes or
   discarding confirmed cash. Direct adapter and participant savepoints are
   forbidden. Access restoration and prepaid-invoice reconciliation remain
   event/resolver consequences; the webhook does not run parallel synchronous
   decision paths.
20. Referral rewards are account credits owned by `financial.credit_notes`;
   neither CRM nor referral services post a parallel wallet balance. Automated
   referral issuance uses the same owner-generated preview, locked confirmation,
   idempotency, audit, and exact funding-ledger evidence as other credit issuance.
21. `financial.account_credit_deposits` owns the typed Deposit Account Credit
   intent and atomic provider-confirmation composition. Customer verification
   and reconciliation enter its typed owner-managed settlement command;
   payment-webhook and payment-proof owners use only its typed flush-only
   participant inside their wider evidence transactions. The full receipt first
   becomes payment-backed unallocated account credit and grants no prepaid
   duration. `financial.account_credit_applications` then owns deterministic
   oldest-debt consumption through `financial.payments` allocation preview and
   confirmation. Only after that debt application completes does the chained
   `account_credit.deposited` consequence ask
   `financial.prepaid_service_renewals` to fund a currently due prepaid period,
   followed by canonical access reconciliation. Customer routes, provider
   webhooks, payment-proof review, invoice issuance/void, and reconcilers are
   adapters around those owners. Callers cannot choose commit behavior, pass a
   transport-shaped gateway object, maintain a wallet counter, allocate rows
   directly, or restore access merely because cash was deposited.
22. Every money-moving financial command is previewed by the same owner that executes it.
   Execution locks and recomputes the preview, rejects stale confirmation,
   records idempotency and actor audit evidence, and structurally links the
   command result to its exact ledger transaction(s). Financial settlement may
   request access reconciliation, but it never promises restoration itself.
23. `financial.collection_accounts`
   (`app.services.billing.collection_accounts`) owns Dotmac receiving-account
   identity, full customer-presented bank details, derived last-four digits,
   active lifecycle, external accounting mapping, and explicit presentment
   order. Portal, reseller, API, invoice, settings UI, payment-proof, and
   attribution adapters carry its identity; they do not maintain bank-detail
   copies. `financial.payment_routing` separately owns health-aware gateway
   ordering. `payment_channels` and `payment_channel_accounts` classify where
   recorded money arrived and never become the gateway-presentment policy.
   Legacy direct-transfer and company-info bank settings are a temporary frozen
   rollback snapshot during A1 verification, not a runtime fallback, and are
   deleted at the contract gate. `accounting_code` fields are external mappings,
   not a Sub chart of accounts or ledger.
24. `financial.payment_configuration_staff_actions`
   (`app.services.payment_configuration_staff_actions`) owns reviewed staff
   lifecycle/default decisions across collection accounts, settlement channels,
   and channel-to-account attribution mappings. Settings routes render its
   exact impact preview and submit its command; confirmation locks and
   recomputes state, rejects a stale fingerprint, applies configuration plus
   audit atomically, and never changes connector-backed checkout routing.
   `payment_channel_accounts` is the sole channel-to-account mapping after
   migration `418_payment_channel_mapping_sot`; the duplicate channel pointer,
   direct toggle routes, and lifecycle/default form fields are retired.

Account adjustments and add-on purchase debits use one evidenced contract:

- Old paths: the generic ledger API could post or reverse arbitrary account
  entries, plan changes posted their own ledger debit, and customer add-on
  purchases derived a wallet balance before constructing a bare adjustment row.
  None recorded a durable decision-to-transaction link.
- New debit owner: `financial.account_adjustments` exposes prepaid funding,
  postpaid receivables, collection-blocking balance, and service-access
  consequence as distinct preview fields. Confirmation locks the account,
  recomputes the preview, rejects stale or unfunded requests, records
  idempotency and actor audit evidence, and links one decision to one exact debit.
  Direct API confirmations enter typed owner commands on transaction-free
  sessions. Plan-change, add-on, and renewal owners use separately named typed
  staging collaborators that flush only inside their wider transaction; no
  caller selects a commit mode. An omitted request currency resolves only from
  `control.settings_spec`'s `billing.default_currency`; the owner carries no
  parallel currency default.
- Credit boundary: the adjustment contract is debit-only. Customer credits,
  including the credit side of a prepaid plan change, remain documents owned by
  `financial.credit_notes`; callers cannot use a generic adjustment as a second
  credit authority.
- Add-on boundary: `financial.addon_purchases` combines the current catalog
  price and subscription state with the adjustment owner's funding preview.
  Mobile/API confirmation sends the fingerprint and an idempotency key, then the
  entitlement and exact adjustment link commit atomically. Clients do not
  derive affordability from a displayed balance.
- Reversal boundary: generic ledger reversal is gated. An adjustment reversal
  is separately previewed and confirmed, preserves the original category,
  records audit/idempotency evidence, and structurally points its exact credit to
  the debit it reverses. It does not promise restoration or mutate access state.
- Evidence and event boundary: successful non-replay debit and reversal
  commands stage PII-free `account_adjustment.confirmed` or
  `account_adjustment.reversed` events with the exact ledger link. Structural
  evidence inspection compares every decision row with its linked append-only
  ledger rows and fails closed on mismatches. The billing alignment audit found
  zero historical adjustment-debit drift, so no inferred monetary backfill is
  authorized; any future mismatch requires reviewed finance evidence rather
  than amount, date, or memo matching.
- Cutover gate: generic ledger writes/reversals remain disabled; plan-change and
  add-on paths contain no direct debit writer; stale preview, insufficient
  funding, idempotent replay, exact debit/reversal links, audit/event atomicity,
  drift inspection, architecture, API, and mobile contract tests must remain
  green.

Immediate plan changes use the same evidenced wrapper contract:

- Old wrapper: customer web/mobile/API and admin could show a proration quote,
  then submit only the target offer. The nested debit owner recomputed safely,
  but nothing proved which wrapper quote the person confirmed, and the change
  request did not name the resulting adjustment, credit note, or ledger row.
- New owner contract: the quote exposes one fingerprint plus distinct prepaid
  funding, postpaid receivables, collection-blocking balance, exact ledger type,
  source and amount, and the explicitly non-restorative access consequence.
  Confirmation supplies that fingerprint and an idempotency key. The owner
  locks and recomputes before changing money.
- Exact evidence: revision `302_plan_change_confirmation_evidence` links the
  applied request to at most one account adjustment or credit note and directly
  to its exact ledger entry. Zero-money immediate changes record the confirmed
  snapshot and no ledger link. Actor audit, request state, subscription state,
  and nested financial evidence commit together.
- Historical boundary: pre-cutover and scheduled next-cycle requests retain
  NULL confirmation/evidence fields; no amount, memo, or timestamp matching is
  used to invent financial provenance.
- Batch boundary: bulk admin changes schedule at each service's next cycle.
  Immediate batch execution is rejected until it can carry per-subscription
  owner previews, fingerprints, idempotency, and results.

Credit-note application is the first migrated financial-action contract:

- Old path: the invoice template derived credit availability and settlement
  totals, then posted directly to an unpreviewed application command.
- New owner: `financial.credit_notes` resolves choices, preview, eligibility,
  locked execution, idempotency, and application-to-ledger evidence;
  `financial.invoices` owns the receivable summary and settlement handoff.
- Cutover gate: preview fingerprint, exact ledger link, audit metadata,
  idempotent replay, invoice-summary, access-reconciliation, and template
  boundary tests must remain green.
- Historical application rows are not heuristically linked to ledger entries;
  reconciliation must use reviewed evidence rather than amount/memo guesses.

Credit-note issuance and voiding are the next migrated financial-action contract:

- Old owners: admin, refund, cancellation-proration, prepaid plan-change, CRM,
  and remediation paths could construct issued documents directly; some posted
  a separate credit ledger row and some posted no ledger evidence at all.
- New owner: `financial.credit_notes` produces the issue/void preview, locks and
  rechecks confirmation, creates the document and descriptive line, requests
  the exact append-only funding or reversal transaction, records idempotency and
  audit evidence, and structurally links every result.
- Projection boundary: the issued credit-note document owns the customer
  financial-position effect. Credit-note funding, application-transfer, and
  void-reversal ledger rows are operational evidence and are excluded from that
  projection so the same credit is not counted twice.
- Application boundary: applying a structurally funded note also links the exact
  unallocated debit that consumes the operational credit pool. Historical notes
  without reviewed funding evidence retain their legacy application behavior.
- Verification phase: direct writers have migrated to the owner and architecture
  tests reject new document, line, or status writers outside the owner package.
- Cutover gate: issue/void preview fingerprints, idempotent replay, actor audit,
  exact funding/application/reversal links, customer-position non-duplication,
  access separation, and adapter-boundary tests must remain green.
- Historical reconciliation is explicit and dry-run-first. It never guesses a
  ledger link from amount or memo; an operator must select the exact entry or
  explicitly approve creation of missing funding for the remaining amount.

Payment-provider observations are a migrated ownership contract:

- Old owner/path: `billing.providers` mixed provider configuration with event
  admission, accepted a caller-selected trust boolean, committed inside a
  helper, and treated reused identity as sufficient replay proof. The
  administrative endpoint could therefore submit payment-state evidence.
- New owner: `financial.payment_provider_events` owns typed observation
  admission, persisted trust source, normalized status and money fields, exact
  evidence digest, provider/event locking, processing result, audit, and
  versioned event evidence. Provider configuration remains separate under the
  existing `financial.payment_routing` owner and is not a provider-event writer.
- Trust boundary: administrative ingestion is non-financial. Only the named
  signature-verified webhook and gateway-verification coordinators call their
  scope-checked, flush-only participants. Refund and reversal evidence remains
  restricted to the verified-webhook boundary.
- Replay boundary: provider plus external or idempotency identity selects one
  canonical record. Administrative and provider-verified trust classes never
  converge. Signature-verified webhook and gateway-verified observations may
  converge only when every normalized decision field has the same exact digest;
  transport-specific raw payload differences carry no financial authority.
  Conflicting evidence fails closed; a legacy incomplete record may resume once
  from verified evidence and receives canonical provenance.
- Policy boundary: this owner records observed gross, provider fee, net,
  currency, reference, and status; it does not choose the provider settlement
  fee equation. Verified invoice settlement requires explicit normalized net
  evidence. The separate gross/net/fee business-policy decision remains with
  `financial.payments` and Finance.
- Cutover gate: typed owner/query contracts, scope rejection, exact replay and
  conflict behavior, atomic audit/event rollback, HTTP-neutral service code,
  PostgreSQL concurrent first-insert serialization, migration, and caller
  architecture tests must remain green.

Payment refunds are the next migrated financial-action contract:

- Old paths: the admin button and provider-event adapter could flip payment
  status without a confirmed amount, preview, idempotency key, or structural
  link to the refund transaction. The compatibility command could also grant a
  cash refund and credit note for the same amount.
- New owner: `financial.payments` exclusively resolves refund capability,
  previews customer funding, unallocated account credit, invoice receivables,
  exact ledger results, and the access-reconciliation handoff; then locks and
  recomputes those facts before confirmation.
- Provider boundary: manual recording is limited to non-provider payments.
  Provider-backed refunds require a signature-verified, provider-matched event
  carrying a normalized amount and currency; the provider-event adapter submits
  that observation and never sets refund status itself.
- Projection boundary: the payment document owns the refund's customer-position
  effect. Its payment-linked refund ledger row is exact accounting evidence and
  is not debited again from unallocated account credit. A separate structurally
  linked internal debit consumes only the refund portion attributable to
  spendable account credit and is excluded from the customer ledger projection.
- Access boundary: refund confirmation requests the canonical account-status
  recheck. Neither the preview nor the UI promises suspension, restoration, or
  any other service-access outcome.
- Cutover gate: stale-preview rejection, idempotent replay, audit evidence,
  exact total and account-credit ledger links, proportional invoice effects,
  normalized provider-event evidence, UI boundary, and owner-writer tests must
  remain green. Refund-plus-credit-note double benefit remains rejected.
- Historical reconciliation is dry-run-first and identifies every unlinked
  refund ledger row. Execution requires an operator-selected exact row and an
  explicitly reviewed account-credit consumption amount; it does not infer
  either from UI balances, memo text, or today's eligibility.

Payment reversals and chargebacks are a separate migrated financial-action
contract; they are not failed captures or customer refunds:

- Old owner/path: the compatibility command combined status mutation and a
  refund-shaped ledger row. It had no preview, confirmation fingerprint,
  idempotency reservation, audit record, or structural reversal evidence; a
  partially refunded payment could be marked failed without reversing its
  remaining settled value, and unallocated credit could remain spendable.
- New owner: `financial.payments` exclusively resolves reversal capability,
  previews the remaining settled value after completed refunds, separates
  customer funding, unallocated account credit, and invoice receivables, then
  locks and recomputes those facts before writing one `PaymentReversal` and its
  exact ledger links. The terminal payment state is `reversed`, distinct from a
  failed capture and from `refunded`/`partially_refunded`.
- Provider boundary: manual recording is limited to non-provider payments and
  represents a chargeback or bank reversal already confirmed outside Sub.
  Provider-backed reversal requires a verified, provider-matched event with the
  explicit normalized `reversal_confirmed` financial effect, exact remaining
  amount, and matching currency. Raw event names or UI-selected statuses are not
  financial evidence.
- Projection boundary: the reversed payment document removes its remaining
  settled value once from customer financial position. Its payment-linked total
  reversal debit is exact accounting evidence and is excluded from both the
  customer-position projection and the unallocated-credit pool. A second,
  structurally linked internal debit consumes only reversal value that was still
  spendable as account credit.
- Access boundary: confirmation requests the canonical account-status recheck;
  payment reversal does not decide, promise, or render a suspension or
  restoration amount.
- Verification/cutover gate: distinct status presentation, stale-preview
  rejection, idempotent replay, actor audit, exact total and account-credit
  links, proportional receivable reopening, normalized provider evidence,
  adapter boundaries, and sole-writer tests must remain green. Generic status
  edits and provider adapters cannot write `reversed` directly.
- Historical reconciliation is explicit and repairable. Inspection reports
  unlinked candidate debits, while execution requires the exact selected row and
  a reviewed account-credit consumption amount. It does not guess from an old
  failed status, a memo, or a current UI balance.

Payment creation, settlement, and allocation are one coherent owner contract:

- Old path: constructing a payment immediately posted allocations, unallocated
  credit, events, and access consequences even when the document said
  `pending`, `failed`, or `canceled`. Generic status edits later treated
  `succeeded` as a field value, provider adapters constructed allocations, and
  the admin form used a browser confirmation instead of an owner preview.
- New owner: `financial.payments` separates payment intent/observation from
  confirmed settlement. Pending, failed, and canceled documents post no money,
  change no receivable, emit no payment-received event, and request no access
  consequence. Only settlement writes `PaymentSettlement`, allocation ledger
  links, an unallocated-credit link, optional prepaid-renewal debit evidence,
  actor audit, and the access-reconciliation handoff.
- Position boundary: the preview keeps confirmed funding, unallocated account
  credit, postpaid invoice receivables, prepaid service renewal, payment state,
  and service-access consequence visibly distinct. A prepaid renewal is an
  explicit previewed debit and billing-period consequence, never a UI-derived
  balance or billing date.
- Allocation boundary: applying already-settled unallocated credit to an
  invoice is a transfer, not new funding. Confirmation writes and structurally
  links the exact invoice credit and a separate internal account-credit debit;
  customer financial position excludes that internal debit so the transfer
  does not double-change total funding. Provider adapters and APIs call the
  same owner.
- Reconciliation boundary: native unallocated-credit reconciliation is an
  orchestration adapter, not a money writer. For each payment/invoice transfer
  it calls the same allocation preview and fingerprint-bound confirmation with
  a stable idempotency key. It never constructs `PaymentAllocation` or
  `LedgerEntry` rows. Only active succeeded payments with reviewed settlement
  evidence are spendable; historical or imported credits without that evidence
  remain visible as unbacked for explicit review.
- Immutability boundary: evidence-backed payment amounts, currencies,
  settlements, and allocations are not edited, deleted, or re-pointed in
  place. Pending allocation intent has no money evidence and may be withdrawn.
  Generic import rollback cannot delete financial rows; imported-payment
  reversal uses the separate batch owner below.
- Provider boundary: verified provider success is a settlement origin, while a
  non-success webhook remains an observation. A verified invoice hint becomes
  pending intent before settlement or uses the confirmed allocation-transfer
  owner after settlement; the provider adapter never constructs financial rows.
- Cash-first provider boundary: a signature-verified webhook or successful
  provider verification commits the payment document, gross charge, provider
  fee, net `PaymentSettlement`, and exact net unallocated-credit ledger link
  before invoice allocation is attempted. Invoice eligibility, prepaid funding
  projection, or other downstream consequence failures cannot roll back that
  confirmed cash evidence.
- Receipt projection boundary: customer receipts and payment-success views use
  the payment owner's application summary. They distinguish gross cash received
  from net customer value credited after provider fees, invoice applications,
  canonical prepaid-renewal outcomes, and remaining payment-backed credit.
  New settlements never write service debits or entitlements. Historical inline
  settlement fields remain immutable evidence and are projected as a fallback;
  a matching canonical outcome is not double counted.
  Legacy payments without structural settlement evidence retain their bounded
  amount-minus-allocation display and are marked unevidenced by the projection.
- Allocation-exception boundary: applying the net unallocated credit to the
  checkout invoice remains owned by the normal preview/fingerprint-bound
  allocation service. Failure leaves the net credit untouched and writes one
  idempotent `PaymentAllocationReconciliationException` linking the payment,
  intended invoice, checkout intent/reference, and error. A successful replay
  resolves that exception; retries cannot duplicate money or exception rows.
- Invoice-lifecycle boundary: invoice-payment checkout cannot persist an intent
  for a draft. The checkout adapter first requests the canonical invoice
  lifecycle owner to transition the document from draft to issued, then creates
  the provider intent from the issued document.
- Historical boundary: old succeeded payments are not automatically trusted or
  linked by amount/memo similarity. Inspection lists candidates; reconciliation
  requires an operator-selected exact ledger row for every active allocation,
  remainder, and prepaid debit, verifies the complete payment partition, links
  evidence, records audit, and posts no new money.
- Prepaid renewal boundary: `financial.payments` ends after confirmed cash,
  invoice allocation, and unallocated-credit evidence are committed. The durable
  `payment.received` event invokes `financial.prepaid_service_renewals`, which is
  the sole decision owner of prepaid period funding, entitlements, billing-anchor
  advancement, and `prepaid_service.renewed` outcomes. Invoice-funded periods use
  the exact fully paid and fully settlement-backed prepaid invoice as their
  customer-position debit;
  invoice-less periods use the owner's exact adjustment debit. Access enforcement has an
  explicit dependency on that owner, while customer notifications and external
  delivery remain independent fanout consequences. The former inline payment
  renewal, operator-selected legacy cycle repair, and plan-driven gap reconciler
  are retired; historical gaps use only
  `financial.prepaid_service_coverage_reconciliation`, which creates missing
  entitlement evidence from an exact existing debit or paid invoice line and
  quarantines ambiguity without posting money.
- Retired payment-application evidence boundary: the former
  `PaymentPrepaidApplication` runtime is not a current financial or coverage
  owner. Revision `394_retire_payment_prepaid_applications` renames its physical
  table to `payment_prepaid_applications_archive` so historical payment,
  settlement, ledger, entitlement, period, and access-recheck provenance remains
  intact without an application model or writer. Finance operations owns
  retention; deletion requires a separate reviewed decision. Revision
  `396_payment_prepaid_application_archive` supplies only an empty compatibility
  archive to databases that had already passed the original empty-table-only
  retirement. Revisions 394 and 396 validate the complete archive schema and
  reject missing, ambiguous, or malformed evidence state. Revision
  `397_validate_payment_prepaid_archive` applies the same fail-closed contract
  to databases already stamped at 396. Alembic autogeneration excludes the
  archive so runtime-model retirement cannot propose physical evidence deletion.
- Cutover gate: pending/no-money tests, stale-preview rejection, idempotent
  creation/settlement/allocation replay, exact settlement/allocation/prepaid
  links, provider replay, explicit historical reconciliation, canonical renewal
  event ordering and idempotency tests, exact coverage-reconciliation tests,
  owner-writer architecture tests, and admin/API preview-confirm boundaries must
  remain green. Generic succeeded status edits and direct settled-allocation
  commands remain gated.

Consolidated payment settlement has a separate scoped owner contract:

- Old path: a billing-account payment entered the generic payment creator as
  already succeeded. That path allocated member invoices immediately, mutated
  `BillingAccount.balance` for any surplus without a ledger row, and accepted a
  browser confirmation instead of an owner preview. Provider verification,
  proof approval, reconciliation, reseller checkout, admin, and API callers
  could each enter that parallel path.
- Owner: `financial.consolidated_payments` exclusively owns the exact FIFO or
  explicit member-invoice allocation preview, locked fingerprint confirmation,
  idempotency, actor audit, and settlement evidence. Verified provider facts
  and approved proofs use the same preview-bound owner; generic
  `financial.payments` may record a pending consolidated observation but gates
  a succeeded consolidated write.
- Position boundary: the preview and confirmation keep payment state, each
  subscriber invoice receivable, reseller-held consolidated credit, prepaid
  funding, and service-access consequence distinct. Paying a reseller account
  does not itself decide subscriber access; paid member invoices request the
  existing access-reconciliation owner.
- Ledger boundary: each member-invoice allocation links its exact subscriber
  `LedgerEntry`. Any surplus links one exact
  `BillingAccountLedgerEntry`; `BillingAccount.balance` is only the current
  projection of those consolidated-account transactions and never substitutes
  for ledger evidence or a fake subscriber account.
- Adapter boundary: admin uses a server-rendered preview and a second
  fingerprint-bound confirmation. The API exposes matching preview and confirm
  commands. Provider webhooks, top-up reconciliation, reseller checkout, and
  proof approval treat their verified fact or human approval as confirmation
  but still bind it to the owner-produced fingerprint and stable idempotency
  key.
- Historical boundary: revision
  `318_consolidated_settlement_reconciliation` adds reviewed structural
  provenance for historical consolidated settlements. Inspection lists exact
  subscriber-ledger, billing-account-ledger, and original-cash candidates;
  preview requires the complete payment partition plus exactly one matching
  processed provider event, verified payment proof, or completed top-up intent.
  Confirmation locks and rechecks those rows, links them to one settlement,
  records actor audit and idempotency evidence, and posts no new money. Missing
  or ambiguous cash provenance is refused, so a legacy synthesized succeeded
  payment cannot become trusted merely because its allocations add up.
- Drift boundary: recorded `BillingAccount.balance`, ledger-evidenced
  consolidated credit, and their projection drift are shown separately in the
  inspection and preview. Historical settlement reconciliation does not repair
  that drift, change access, or infer any restoration amount, eligibility, or
  billing date.
- Cutover gate: read-only preview, exact dual-ledger evidence, stale-preview
  rejection, idempotent replay, pending/no-money behavior, generic-writer gate,
  provider replay, historical provenance refusal, admin/API boundary, and
  owner-registry tests remain green.

Consolidated-credit allocation is a separate transfer owned by the same scoped
financial service:

- Old path: the reseller portal and API submitted a one-step allocation after
  deriving the maximum from displayed invoice totals and
  `BillingAccount.balance`. The payment service could synthesize a succeeded
  payment when that projection lacked source evidence, then mutate the balance
  directly. The result did not structurally identify which consolidated credit
  was consumed.
- Owner: `financial.consolidated_payments` produces the allocation capability,
  exact FIFO source/invoice preview, locked fingerprint confirmation,
  idempotency, actor audit, and access-reconciliation handoff. Web and API
  adapters only render the owner preview and submit its confirmation command.
- Position boundary: recorded consolidated credit, ledger-evidenced
  consolidated credit, subscriber postpaid receivables, payment state, and
  service-access consequence remain separate. Projection drift or historical
  allocation without exact source-consumption evidence fails closed; no
  synthetic payment repairs it.
- Exact evidence: revision `316_consolidated_credit_allocation` records one
  allocation decision linked to its billing-account debit and item rows linking
  each source billing-account credit to the exact `PaymentAllocation` and
  subscriber `LedgerEntry` it produced. `BillingAccount.balance` is updated only
  alongside the canonical ledger transaction.
- Historical boundary: revision
  `323_consolidated_credit_consumption_reconciliation` adds reviewed provenance
  for a legacy transfer that changed a member receivable without recording the
  exact source-consumption structure. Inspection keeps the recorded balance,
  ledger-evidenced credit, projection drift, valid source credits, unlinked
  member allocations, and unclaimed debit candidates separate. Preview and
  confirmation require operator-selected exact source credit, payment
  allocation, subscriber ledger result, and either one exact existing
  billing-account debit or explicit approval to append the missing debit.
  Confirmation locks and recomputes the evidence, is fingerprint-bound and
  idempotent, records actor audit, and never changes `BillingAccount.balance`.
- Repair boundary: a missing debit may be appended only up to the exact negative
  projection drift and only for allocations carried by a payment with an exact
  consolidated settlement. Positive/unbacked projection value, an allocation
  without an exact subscriber ledger result, an unsettled or synthesized carrier,
  cross-reseller evidence, and ambiguous source consumption remain fail-closed.
  Neither inspection nor confirmation changes or promises service access.
- Cutover gate: the projection-only balance credit/debit helpers and legacy
  one-step allocation command remain gated. Read-only preview, stale rejection,
  cross-reseller scope, partial allocation, historical existing/missing-debit
  reconciliation, unbacked-credit refusal, exact dual-ledger links, replay,
  audit, API preview-confirm, and sole-writer architecture tests must remain green.

Consolidated refunds and payment reversals remain under that scoped owner:

- Old path: the subscriber payment refund/reversal owner rejected every
  billing-account payment. Admin/API confirmation and normalized provider
  refund or reversal events therefore stopped without an authoritative money
  path, while ad hoc balance repair risked assigning reseller money to a fake
  subscriber or leaving paid member receivables closed.
- Owner: `financial.consolidated_payments` owns refund and reversal capability,
  preview, locked fingerprint confirmation, idempotency, actor/provider audit,
  payment state, and every resulting ledger link. Generic
  `financial.payments` continues to own subscriber-scoped returns and refuses
  consolidated scope.
- Position boundary: reseller-held credit, member invoice receivables, payment
  refund/reversal state, and service access remain separate in preview and
  confirmation. A partial refund may consume only credit still evidenced for
  that payment at billing-account scope. A partial request that would infer an
  allocation clawback fails closed; a complete refund or reversal explicitly
  reopens every remaining allocation.
- Evidence boundary: consolidated credit consumption writes and links one exact
  `BillingAccountLedgerEntry`. Each reopened member allocation writes an exact
  invoice-linked subscriber debit and records its source `PaymentAllocation`
  through `ConsolidatedPaymentReturnAllocationEvidence`. No fake subscriber,
  UI-derived balance, mutable-only restoration amount, or unlinked ledger row
  is permitted.
- Access/provider boundary: member receivable reopening emits a reconciliation
  request but does not decide suspension or restoration. Trusted normalized
  provider events dispatch to the same consolidated owner; untrusted API
  callers cannot claim provider evidence.
- Cutover gate: revision `317_consolidated_payment_returns.py`, partial-surplus,
  full-refund, reversal, stale-preview, replay, dual-ledger, provider dispatch,
  admin/API dispatch, and sole-writer tests must remain green.
- Historical boundary: revision `324_consolidated_return_reconciliation` adds
  read-only inspection, fingerprint-bound preview, locked confirmation,
  idempotent replay, actor audit, and one reviewed provenance row for an
  existing historical consolidated `PaymentRefund` or `PaymentReversal`.
  Confirmation requires the return amount to be exactly partitioned across the
  selected billing-account debit and selected inactive allocation debits, and a
  provider-backed return additionally requires its exact processed normalized
  event. It links those existing rows and may correct the payment's derived
  refund/reversal state from the exact return documents; it creates no return
  document or ledger transaction, does not change `BillingAccount.balance`, and
  makes no service-access decision.
- Refusal boundary: recorded and ledger-evidenced consolidated credit must
  agree before return evidence is linked. Active allocations, incomplete or
  reused evidence, subscriber-wallet or generic return carriers, owner-confirmed
  rows with missing evidence, and ambiguous provenance remain fail-closed rather
  than being reconstructed from a UI value, memo, current eligibility, or
  inferred billing state.
- Missing-document boundary: revision
  `325_consolidated_return_document_reconstruction` covers the narrower case
  where a historical consolidated payment already carries a return-compatible
  status and exact unclaimed return debits exist, but the `PaymentRefund` or
  `PaymentReversal` document is absent. The status is a consistency gate, not
  financial evidence. Preview binds a proposed document ID, explicit reviewed
  amount, non-secret external evidence reference, exact selected debit partition,
  and exact
  processed provider event when provider-backed. Confirmation creates only the
  missing return document and reconstruction provenance, then composes the
  revision-324 evidence owner for every structural link and derived payment
  field. It posts no money, changes no billing-account balance, invoice,
  allocation consequence, or access state, and replays idempotently.
- Reconstruction refusal: the selected evidence must derive the same
  `refunded`, `partially_refunded`, or `reversed` state already recorded on the
  historical payment. Succeeded/failed status, missing or synthetic settlement,
  projection drift, an existing reversal, incomplete/reused evidence, or an
  existing return document that has not completed revision-324 reconciliation
  blocks reconstruction. No amount, type, or source reference is inferred from
  the historical status. Bank rows, narrations, account details, credentials,
  and other raw statement data are never stored in the reference.

Imported-payment batch reversal is a separate migrated wrapper owner:

- Owner: `financial.import_payment_batch_reversals` owns durable creation
  provenance, batch eligibility, the human preview fingerprint, locked
  confirmation, batch idempotency, actor audit, and exact links from import row
  to source settlement to resulting `PaymentReversal` and ledger transactions.
  `financial.payments` remains the sole writer of every nested payment reversal.
- Provenance boundary: a new applied payment row records both its exact
  `payment_id` and whether that run created or merely reused the payment. The
  created Payment also links back to that run. A later idempotent import cannot
  claim or reverse a payment created by an earlier run.
- Historical boundary: nullable provenance is deliberate. Existing import rows
  without both structural links fail closed and are not backfilled from row
  JSON, external ID, amount, memo, file name, or current UI state.
- Preview boundary: the batch owner resolves every exact source settlement,
  allocation ledger link, unallocated-credit link, prepaid-funding link,
  remaining reversible amount, receivable reopening, and resulting reversal
  ledger debit. Prepaid funding, unallocated account credit, postpaid
  receivables, collection-blocking balance, payment state, and service-access
  consequence remain visibly distinct.
- Confirmation boundary: the owner locks the import run and every affected
  account, rebuilds the whole batch preview, rejects drift before posting, then
  composes idempotent per-payment reversal commands in one transaction. A
  changed payment, refund, allocation, funding position, receivable, or source
  evidence aborts the entire batch. No imported row is deleted or deactivated.
- Reused-row boundary: rows that structurally say `record_created = false` are
  shown as skipped and remain unchanged. A batch with no newly created payments
  is ineligible rather than reversing somebody else's payment.
- Result/access boundary: `PaymentImportBatchReversalItem` links each import row,
  source settlement, payment reversal, exact result ledger debit, and optional
  account-credit-consumption debit. Nested payment reversal reopens receivables
  and requests canonical account/access reconciliation; the batch UI never
  promises restoration, suspension, or an eligibility amount.
- Cutover gate: revision `303_payment_import_batch_reversal.py`, stale-preview,
  replay, atomic multi-payment, mixed created/reused, invoice reopening,
  historical fail-closed, exact-evidence, adapter, and sole-writer tests must
  remain green. Legacy settings-history rollback stays nonfinancial only.

Nonterminal invoice lifecycle transitions are owned alongside terminal closure:

- Old paths: scheduled billing and usage posting constructed invoice documents
  or lines directly, scheduled billing temporarily flipped prepaid drafts to
  issued, prepaid credit reconciliation and cleanup moved invoices back to
  draft, and overdue automation, dunning, and admin bulk issue assigned status
  and timestamps themselves. The architecture allowlist normalized these
  parallel writers instead of enforcing one owner.
- Owner: `financial.invoices` now stages automation-created invoice documents,
  validates and stages automation/usage invoice lines, owns stable billing-line
  replay, owns draft issuance, rechecks whether an untouched prepaid receivable
  may return to draft, and owns overdue eligibility, transition, one-time
  observation event, and audit. Automation, usage, reconciliation, cleanup,
  dunning, and UI services select candidates and call the owner.
- Construction boundary: only `app.services.billing.invoices` may construct
  `Invoice` or `InvoiceLine` rows. System staging accepts only draft/issued
  documents, records the source reason and exact document amount, and rejects a
  billing-line key reused for different facts. Document staging posts no ledger
  transaction; the invoice source document remains the canonical receivable
  fact and its customer-ledger projection is derived from that exact row.
- Derived-state boundary: payment and credit settlement still derive
  `paid`/`partially_paid`/reopened status inside the invoice owner package from
  canonical settlement facts. No adapter may assign those states. Draft,
  issued, and overdue transitions record that no ledger transaction resulted;
  terminal monetary closure continues to require exact evidence below.
- Access boundary: `invoice.overdue` is an observation. It does not create a
  dunning consequence or decide service access. Returning an unfunded prepaid
  invoice to draft likewise changes no funding and grants no access.
- Verification boundary: invoice and invoice-line constructors are restricted
  to `app.services.billing.invoices`; the lifecycle writer allowlist contains
  only that owner and its derived-total helper. Direct construction or status
  assignment in automation, usage, reconciliation, cleanup, collections, and
  web adapters is rejected by architecture tests.

Invoice void and write-off are distinct terminal owner contracts:

- Old path: generic invoice status edits, single/bulk routes, and prepaid
  remediation jobs could set `void` or `written_off` directly. Void constructed
  ad hoc credits and deactivated original debits, violating append-only ledger
  semantics; partially settled invoices could retain stranded payment or credit
  allocations. Write-off trusted the stored balance and had no structural link
  from the terminal decision to its adjustment entry.
- New owner: `financial.invoices` exclusively resolves void/write-off
  eligibility, derives the receivable from invoice total plus canonical payment
  and credit-note settlement facts, previews exact consequences, locks and
  rechecks confirmation, records idempotency/audit evidence, writes one terminal
  `InvoiceClosure`, and links every exact ledger result through
  `InvoiceClosureLedgerEvidence`.
- Meaning boundary: void means the invoice should never have existed and is
  permitted only after effective payment/credit value is removed through its
  own owner. It reverses each original invoice debit append-only and leaves the
  original active. Current `invoice`-source debits and historical
  `adjustment`-source debits qualify only when the ledger row carries the exact
  `invoice_id`; unlinked account adjustments remain outside this owner.
  Write-off means collectible postpaid debt will not be
  collected; it writes one exact adjustment credit for the remaining
  receivable. It is not payment, prepaid funding, customer credit, or invoice
  void.
- Position boundary: a native written-off invoice remains the original customer
  debit and its `InvoiceClosure` contributes only the confirmed remaining-debt
  credit, preserving any already-applied payment/credit value. The linked
  operational ledger entry is evidence and is not counted a second time. Void
  removes the invoice document from customer position; its reversal rows are
  likewise evidence-only. Historical evidence reconciliation changes neither
  projection.
- State boundary: generic create/update/delete paths cannot manufacture paid,
  partially-paid, void, or written-off state, edit `balance_due`, or delete an
  issued receivable. Admin and API adapters use owner preview/confirmation;
  bulk void displays and confirms each per-invoice owner preview. Prepaid repair
  and cleanup workflows call deterministic system confirmation rather than
  mutating terminal state.
- Access boundary: the preview names only an access-reconciliation handoff.
  Confirmation clears the receivable and asks the access/collections owners to
  re-evaluate eligibility; neither void nor write-off promises restoration.
- Historical boundary: legacy terminal invoices remain immutable. Inspection
  lists exact invoice-linked ledger candidates; reconciliation requires explicit
  operator-selected evidence (one exact write-off credit or one exact reversal
  for every original invoice debit), records links/audit, and posts no money.
- Cutover gate: append-only reversal, exact write-off link, no-settlement void,
  stale-preview rejection, idempotent replay, draft/no-money closure, explicit
  historical reconciliation, remediation-adapter, admin/API confirmation, and
  owner-writer architecture tests must remain green.

Dunning decisions and their service-access consequences are now a distinct,
evidenced owner contract:

- Old paths: scheduled dunning selected policy steps, `_execute_dunning_action`
  independently rechecked some gates, `_suspend_account` rechecked a different
  set, payment events decided whether restoration was allowed from invoice
  snapshots, invoice-overdue events maintained a second warning/shield path,
  and case resolution restored throttle state without evidence linking the
  decision to what changed.
- Decision owner: `financial.dunning` owns postpaid policy selection and the
  shared financial access consequence preview/confirmation used by dunning,
  prepaid enforcement, payment settlement, and billing reconciliation.
  `financial.access_resolution`, payment arrangements/proofs/extensions, and
  billing health supply independent decision inputs; none writes access state.
- Grace owner: `app.services.collections.grace_policy` resolves the effective
  duration and provenance once: explicit account override, then active policy
  set, then billing-mode default. Postpaid dunning steps count from the end of
  that grace decision; prepaid planning, enforcement, and customer status use
  the same low-balance deadline. Zero configured days means no elapsed-time
  grace and is actionable immediately. The former collections settings
  `prepaid_grace_days` and `prepaid_deactivation_days` are retired.
- Consequence writer: `access.subscription_lifecycle` exclusively creates or
  resolves `EnforcementLock` rows, persists their `access_mode`, and derives
  subscription/account status.
  RADIUS and session-enforcement services project that lifecycle result; they
  do not decide whether debt, funding, a shield, or a case permits access.
- Timer-state writer: `financial.prepaid_enforcement_state`
  (`app.services.prepaid_enforcement_state`) exclusively arms and clears the
  Subscriber low-balance and deactivation timestamps. The enforcement planner,
  scheduled sweep, funded-restoration flow, and account lifecycle submit
  prepared observations or cleanup requests; they do not assign the fields.
  Disabled and canceled accounts clear obsolete timers in the lifecycle
  transaction. The timer writer flushes but never commits and owns no funding,
  grace, suspension, restoration, or eligibility decision.
- Evidence boundary: every confirmed financial suspend, reject, throttle, or
  restore writes one `FinancialAccessConsequence` containing the exact locked
  preview fingerprint, idempotency key, separated receivable/prepaid/profile/
  shield/health inputs, outcome, and system audit. Structural evidence links
  the decision to every exact enforcement lock, access credential, and dunning
  case it created, resolved, throttled, or restored. A `DunningActionLog` links
  to the consequence that implemented its access action.
- Access-tier boundary: hard reject is the default. Captive is a requested
  exception only for an explicitly opted-in, direct-house account with an
  explicit residential classification and a valid enabled portal network
  contract. Business, government, NGO, reseller-owned, reseller-principal,
  system, disabled, canceled, and uncategorized accounts fail closed even when
  a stale opt-in flag exists. `app.services.walled_garden_policy` revalidates
  persisted captive intent and applies most-restrictive-active-lock-wins before
  RADIUS, connectivity, and UI read projections consume it.
- Captive cutover gate: the global captive setting remains disabled until
  staging proves RADIUS projection readback, portal reachability from the
  restricted tier, a real test payment, and canonical post-payment access
  restoration. Any failed or stale readiness input downgrades the effective
  tier to hard reject.
- Restoration boundary: payment and invoice settlement submit observations;
  they never promise restoration. Confirmation resolves overdue locks/cases/
  throttle only after canonical overdue receivables are empty, and resolves
  prepaid locks/timers only after the canonical funding threshold is met.
  Other active lock reasons remain untouched.
- Financial-position boundary: post-cutover account positions and reconstructed
  prepaid funding share the same currency-typed signed native-event arithmetic.
  Overdue decisions consume collectible receivables; prepaid decisions consume
  the reviewed opening position plus native events and compare it with the
  affordability threshold. The archived Splynx mirror is migration evidence,
  never a runtime fallback. Reason-scoped repair follows the reason owner: an
  ``overdue`` lock is never judged by the prepaid affordability resolver.
  A paid prepaid subscription invoice is excluded from collectible AR but is
  included once as consumed service value only when exact active payment and/or
  credit-note applications fully back its total. Paid status alone and imported
  line-less invoices are not sufficient. When the exact same period is already represented by an
  unreversed renewal adjustment and active debit-backed entitlement, that
  canonical debit takes precedence and the documentary invoice contributes no
  second position effect. Scalar and bounded-cohort reads use the same rule.
  Ledger projections require both `is_active` and `affects_customer_position`.
  Those fields are deliberately orthogonal: `is_active` follows the source
  artifact lifecycle, while `affects_customer_position` prevents structural,
  cutover, and correction evidence from duplicating document or opening-baseline
  value. Neither field may be inferred from or mechanically rewritten from the
  other.
- Transport boundary: `invoice.overdue` is observation only. Notifications,
  throttle, suspension, and rejection come from the configured dunning step.
  `payment.received` always asks the owner to reconcile and contains no local
  invoice-balance eligibility branch.
- Retired controls: billing automation no longer reads or seeds
  `auto_suspend_on_overdue`, `suspension_grace_hours`,
  `dunning_escalation_days`, `blocking_period_days`, or
  `deactivation_period_days`. Existing database rows are inert legacy data.
  The hourly billing-notification job sends pre-due invoice reminders only;
  it does not re-emit overdue events or maintain invoice metadata as access
  evidence. Policy dunning steps are the only overdue timing/action controls.
  `PolicySet.suspension_action` is retained as compatibility data but is not an
  execution input and is no longer exposed by the admin policy form.
- UI boundary: subscriber pages do not derive a "next block" date or access
  eligibility from balance, grace, or legacy metadata. They render only
  owner-produced financial/access projections and confirmed consequences.
- Historical boundary: a throttled credential without a structurally captured
  `pre_throttle_radius_profile_id` is reported in preview/audit and is not
  restored by guessing from an offer, UI state, or current profile. It requires
  explicit reviewed historical reconciliation.
- Cutover gate: stale-preview rejection, idempotent replay, exact consequence
  links, canonical receivable evidence, shield/health enforcement, reason-
  scoped restoration, event-adapter thinness, and owner-writer architecture
  tests must remain green.

Rule: no module should infer access from draft invoices, ad hoc balances, or
legacy import fields when a billing/access resolver exists. Celery tasks only
apply scheduling, routing, idempotency, and feature-gate concerns before calling
the owning financial service. Retired VAS archive tables have no application
writer and are excluded from schema autogeneration so history cannot be dropped
accidentally. Templates and mobile clients do not calculate invoice
receivables, credit availability, restoration amounts, billing dates, or
financial-action eligibility.

Tax-accounting migration record:

- Old owner: `app.services.web_reports_extended` queried invoice models and the
  Jinja report interpreted them as `tax_amount`/`total_amount`, ignored its date
  controls, mixed currencies, and labelled issued tax as collected.
- New source-fact owner: `app.services.tax_accounting` projects typed bounded
  invoice, credit-note, and WHT rows plus full filtered aggregates per currency.
  It owns proof-backed WHT source creation, legal WHT transitions, and the WHT
  official timeline. Web services and routes parse and serialize only; they do
  not construct source rows, select transaction completion, audit separately,
  or map unexpected failures into validation messages.
- Accounting owner: Dotmac ERP owns TaxCode configuration and account mappings,
  balanced invoice/credit-note/payment/WHT journals, tax transactions, tax
  returns, and financial statements. Its existing pull integration consumes
  Sub's bounded sync feeds; no parallel push or local Sub subledger is added.
- Read boundary: the tax report is the canonical tax-register projection from
  authoritative invoice, credit-note, and WHT source documents. ERP journals do
  not replace source-document ownership, and Sub report rows do not replace ERP
  accounting.
- Credit-note tax point: `financial.credit_notes` persists the first `issued_at`
  when a credit enters an adjusting state; `financial.tax_accounting` uses that
  timestamp for report periods and the ERP sync contract. Migration 291
  backfills existing issued/applied rows from `created_at`. All direct automated
  writers use the shared lifecycle adapter, and cancellation credits preserve the
  source invoice, rate, and inclusive/exclusive/exempt line treatment.
- Fallback retirement: the false `total_tax`/`invoices` model contract and
  `tax_amount`/`total_amount` template fields are removed by the tax-accounting
  ownership boundary.
- Feed contract: invoice and credit-note sync lines expose `tax_rate_id` and
  `tax_application`; the tax-rate feed exposes code/rate; payment sync exposes
  gross cash settlement, net bank cash, WHT amount/rate/status/record/certificate,
  and the source resolution timestamp for terminal decisions. WHT transitions
  advance the owning payment watermark so ERP re-pulls changes.
- ERP resolution: ERP resolves each source rate/treatment to exactly one active,
  effective, ERP-owned TaxCode and fails closed on missing or ambiguous account
  configuration. Corrections reverse and re-post in one transaction rather than
  mutating posted lines.
- Operator control: `/admin/billing/tax-accounting` is the permission-protected
  source-fact and WHT evidence console with server-side search, status filters,
  counts, and pagination. It lists direct-customer and consolidated-account WHT
  records without assuming every row belongs to a reseller. It does not offer
  account mapping or journal controls.
- WHT lifecycle: payment-proof verification submits exact typed evidence to the
  tax owner's flush-only participant, which creates the pending source record,
  initial timeline, and versioned receivable event in the proof transaction.
- Customer WHT policy and basis: `financial.customer_tax_policies` owns
  per-customer WHT enablement, `control.settings_spec` owns the global admin
  rate, and invoice-linked direct bank transfer uses the invoice owner's
  authoritative VAT-exclusive subtotal as the only automatic WHT basis.
  Arbitrary account-credit deposits and online/card gateway checkout remain
  non-WHT and collect the full invoice amount.
  The tax owner alone permits pending -> certified -> reclaimed and
  pending/certified -> written_off, requires certificate evidence or a write-off
  reason, and appends `withholding_tax_transitions`. The public transition owner
  locks the WHT record then its linked Payment and atomically writes audit and a
  `withholding_tax.status_changed` event. Each transition advances the payment
  sync watermark; ERP applies the accounting consequence from its own mapped
  accounts. Exact state replay is a no-op; conflicting evidence fails closed.

## Customer Context

1. `customer.accounts` owns Subscriber account creation and the
   transaction-neutral preparation command used by approved cross-domain
   coordinators. It delegates requested status to
   `access.subscription_lifecycle` and stages `subscriber.created`; callers do
   not construct Subscriber rows directly. Existing direct constructors remain
   explicit shrink-only migration debt, not approved parallel owners.
2. Customer context owns identity, account, billing, service, support, and
network summary composition.
3. Customer network context owns the raw customer-to-network footprint.
4. Network access path owns the customer service path.
5. `customer.profile_commands` owns admin customer profile edits and explicit
   person-to-business customer conversion. Normal person edit submission must
   not mutate account type; conversion is a dedicated command with its own
   validation and audit trail. `customer.name_repairs` separately owns exact,
   audit-evidenced legacy Subscriber name remediation until Party name
   projection cutover; no webhook, CLI, or generic profile helper writes it.
6. `customer.service_status` owns customer-visible service health and action
   hints, including whether payment can restore every active service hold and
   the authoritative amount required by financial policy.
7. `customer.usage_summary` owns customer usage windows, headline totals, and
   total provenance. An authoritative zero is a valid value, not a missing-data
   sentinel.
8. `customer.reseller_status_actions` (`app/services/reseller_portal.py`) owns
   the reseller-scoped impact preview for deactivate, restore, and disable. It
   evaluates current subscription state, active enforcement locks, duplicate-
   login restore conflicts, account overrides, and accounts with no services,
   then fingerprints that exact preview. The first POST renders a distinct
   server-calculated confirmation page; the second carries the fingerprint and
   an account-bound idempotency key. The owner reserves the key, rechecks after
   locking, commits the lifecycle mutation and replay result once, and returns
   the original result on retry. Lifecycle mutation is still delegated to
   `access.subscription_lifecycle`.
9. `customer.experience_lifecycle`
   (`app/services/customer_experience_lifecycle.py`) owns the read-only typed
   composition of native `Project -> ProjectTask -> WorkOrder -> Ticket` state,
   the customer experience-state projection, and server-owned self-care action
   availability. It never mutates any of those roots. Customer, reseller,
   field, web, and mobile adapters consume it without a CRM mirror fallback.
10. `customer.work_order_selfcare`
    (`app/services/customer_work_order_selfcare.py`) owns subscriber-scoped live
    technician-location reads and the canonical, audited customer technician
    rating. It reads native dispatch assignment and field-presence facts and
    writes no work-order lifecycle state.
11. `subscriber.growth_reports` (`app/services/subscriber_growth.py`) owns the
   admin subscriber growth and churn report reads: monthly growth/churn series,
   month-over-month new counts, churn/at-risk summaries, status counts, and
   cumulative signups. The derived-cancelled rule (explicit `canceled`, or NULL
   status on an inactive row) lives here; report pages compose it and never
   re-derive lifecycle in Python.
12. `customer.data_completeness`
   (`app/services/subscriber_data_completeness.py`) owns the purpose-specific
   requirements, derived completeness/revalidation state, capture backlog, and
   filing-readiness counts. It is read-only: it identifies the gap and never
   fills it.
13. `customer.location_verification`
   (`app/services/geocode_reconciler.py`) is the only writer of subscriber
   location verification-ledger facts and owns reconciliation of a captured GPS
   pin against claimed location. It writes only facts that agree; disagreement
   is flagged for a human and never auto-applied.
14. `customer.location_capture` (`app/services/location_capture.py`) owns the
    default-off rollout controls, source authorization, prompt eligibility and
    snooze lifecycle, and orchestration of field-arrival, portal, and agent
    capture. Those adapters call this owner, which delegates adjudication and
    ledger writes to `customer.location_verification`. Neither owner writes
    `Subscriber` columns; projecting a verified fact onto the profile remains
    the subscriber profile owner's job.

Rule: admin, portal, support, and reporting views should consume context
services instead of rebuilding customer joins. Admin routes submit explicit
profile commands; they do not expose a generic category dropdown that can
silently move an individual into business workflows. Customer clients must not
infer that `blocked` or `suspended` means payment-restorable, or calculate
restoration amounts from locally loaded invoice rows; they consume
`/me/service-status`. Customer clients consume `/me/usage-summary` totals and
provenance; they do not replace a server total with a loaded-session page,
chart-series sum, or a different time window.
For project/task/field/ticket journeys, clients render the identifiers,
relationships, status presentations, experience state, and allowed actions
provided by `customer.experience_lifecycle`; they do not join CRM ids or derive
confirmation, tracking, or rating eligibility from raw statuses.

## Support Operations

1. `support.ticket_lifecycle` is the canonical owner of Ticket creation and
   identity, human-readable number allocation, guarded status transitions,
   timestamps, team/person assignment, comments/mentions/attachments,
   links/duplicates/merges, resolution confirmation/disputes, CSAT, audit,
   official timeline, and transactional events. Local ticket creation reserves
   numbers through the locked `support_ticket` document sequence and advances
   past occupied imported numbers; portal, API, automation, and admin adapters
   never allocate numbers. The retired lifecycle-owner alias is not a
   registered service.
2. `support.ticket_configuration` owns the operator-visible status subset,
   priority/type choices, routing, and SLA policy. A configured status must be
   part of the lifecycle vocabulary.
3. Status configuration does not own labels, tones, icons, or platform colors;
   those are read-side presentation concerns.
4. `support.ticket_bulk_commands` owns exact selected membership, normalized
   shared changes, side-effect-free eligibility preview, confirmation drift
   detection, and structured outcomes for admin ticket bulk update. Eligible
   execution delegates through `app.services.support.Tickets.update`; it does
   not maintain a second status, priority, assignment, SLA, automation,
   work-order, notification, event, audit, or workqueue path.
5. `support.ticket_assignment_rule_configuration` and
   `support.ticket_automation_rule_configuration` own their respective rules.
   `support.ticket_assignment_evaluation` and
   `support.ticket_automation_evaluation` return typed proposals. Policies do
   not mutate Ticket lifecycle state; the lifecycle owner applies accepted
   consequences. Assignment evaluation may advance only its locked round-robin
   cursor in the lifecycle transaction.
6. `support.ticket_sla_clock` retains SLA clock/breach ownership.
   `support.ticket_work_order_handoff` retains issuance and native provenance.
   Field outcomes may add internal evidence but never resolve/close a Ticket.
7. Support and `communications.team_inbox` remain separate owners. No approved
   checked-in workspace contract authorizes unification.

Rule: API, admin, customer, reseller, automation, and import adapters request
ticket mutation through the ticket lifecycle service. Settings may narrow the
choices presented to operators but cannot create a state the lifecycle owner
will reject.

## UI List Projections

1. `ui.list_contracts` owns normalized list query state, list capability
   declarations, page metadata, and canonical URL serialization.
2. Each resource declares one projection owner for its searchable fields,
   filters, stable sort, row projection, and filtered count.
3. `ui.customer_list_projection` is the first migrated resource. The live admin
   customer route and Jinja table consume `ListQuery` and `PageMeta` from
   `app.services.web_customer_lists`.
4. The configurable-table customer data endpoint is now a compatibility
   projection over `app.services.web_customer_lists`. `app.services.table_config`
   still owns saved column visibility/order and serialization, but it does not
   select, filter, count, sort, or paginate customer rows. The live customer
   template does not load or mount the legacy client.
5. Customer configurable-table migration record:
   - Old owner: the generic
     `TableConfigurationService.apply_query_config` customer branch.
   - New owner: `app.services.web_customer_lists`, using `ui.list_contracts`.
   - Verification phase: contract tests exercise canonical scope, compatibility
     aliases, filters, stable sorting, and clamped pagination. A runtime dual-read
     shadow was not retained because the live customer screen had already been
     gated off the legacy client.
   - Cutover gate: customer list, compatibility API, SOT-registry, and route
     architecture tests must remain green.
   - Fallback retirement: the generic customer scalar-filter and location-filter
     branches were removed; unsupported inputs fail closed with HTTP 400.
6. Legacy `q`, `activation_state`, `customer_type`, NAS/location,
   `customer_name` sort, `limit`, and aligned `offset` inputs are normalized into
   `ListQuery`.
7. `ui.subscriber_list_projection` owns the remaining subscriber
   configurable-table query. There is no separate live subscriber list: the
   production admin list and legacy Playwright facade both use `/admin/customers`,
   while `app.web.admin.subscribers` is an import alias to the customer router.
8. Subscriber configurable-table migration record:
   - Old owner: the generic `TableConfigurationService.apply_query_config`
     Subscriber branch.
   - New owner: `app.services.web_subscriber_lists`, using `ui.list_contracts`
     and delegating subscriber scope/full-text search to
     `app.services.subscriber.Subscribers.query`.
   - Verification phase: contract tests exercise scope, search aliases, filters,
     stable sorting, filter-before-pagination, and clamped offsets. No runtime
     shadow was retained because no production template mounts the subscriber
     dynamic-table client.
   - Cutover gate: subscriber service, compatibility projection, SOT registry,
     and architecture tests must remain green.
   - Fallback retirement: the generic table query engine and Subscriber-specific
     fallback were removed. New table data resources require a named projection
     owner before registration.
9. Subscriber list reads are read-only. The retired table path used to generate
   missing subscriber numbers and commit them during serialization. Identifier
   assignment remains with subscriber creation/update workflows; projections
   return the stored value, including `null`, and never repair it implicitly.
10. Legacy subscriber `q`, `status`/`activation_state`, `subscriber_type`,
    declared sorts, `limit`, and aligned `offset` inputs normalize into
    `ListQuery`; undeclared scalar filters and sorts fail closed with HTTP 400.
11. `ui.invoice_list_projection` extends the existing
    `app.services.web_billing_overview` invoice owner with declared searchable,
    filterable, and sortable fields; stable ID tie-breaking; page clamping; and
    an uncapped export scope. Full-page and HTMX reads render the same
    `_invoices_list.html` and `_invoices_table.html` projections, so status
    totals, filters, canonical URLs, pagination, and rows cannot diverge.
12. `ui.support_ticket_list_projection` extends the existing
    `app.services.web_support_tickets` web owner and delegates its filtered
    domain query to `app.services.support.Tickets`. It owns the declared admin
    search/filter/sort capabilities, exact count, page clamping, status-summary
    links, and uncapped CSV scope. Full-page and HTMX reads render the same
    `_list.html` and `_table.html` projections.
13. Support-ticket list migration record:
    - Old owners: the admin route and Jinja fragments independently interpreted
      sort/page inputs, inferred a next page from one extra row, hand-built URLs,
      and applied a silent 10,000-row export cap. Advanced filters submitted by
      the page were not accepted by the export route.
    - New owner: `app.services.web_support_tickets`, using `ui.list_contracts`
      and the canonical filtered query in `app.services.support.Tickets`.
    - Verification phase: contract, query, route/template architecture,
      filter-before-pagination, stable-order, exact-count, clamped-page,
      canonical-URL, accessibility, and complete-export tests protect the
      boundary. A runtime dual-read was not retained because both paths used the
      same database query and the old implementation had no independent owner.
    - Cutover gate: support service, web projection, route/template, SOT
      registry, and focused list tests must remain green.
    - Fallback retirement: the route no longer owns pagination semantics; the
      templates no longer assemble sort/filter/page URLs; the one-extra-row page
      estimate and silent export cap are removed. Legacy `order_by`/`order_dir`
      inputs remain only as canonicalizing compatibility aliases.

14. `ui.reseller_list_projection` (`app.services.web_admin_resellers`) declares the
    admin reseller list capabilities with `ui.list_contracts` — status filter, name
    sort, pagination — so the route derives no pagination or filter rules;
    `web_admin_resellers` owns the reseller read. The reseller admin surface is
    granularly gated by `reseller:read` (list) and `reseller:write` (create/edit),
    split off the shared `customer:read`/`customer:write`; migration preserves access
    by granting the reseller permissions to current customer-permission holders.
15. `ui.work_order_list_projection` (`app.services.web_dispatch_work_orders`)
    declares the admin work-order list capabilities with `ui.list_contracts` and
    delegates the read to `work_order_views.query_work_orders`
    (`operations.work_orders`), which owns the canonical filtered/sorted query —
    the projection issues no SQL. Read-only: no Sub-owned admin bulk command is
    declared, so no selection/bulk is declared. Each dispatch route is granularly
    gated (`operations:dispatch:read`/`:write`/`:assign`).

16. `ui.project_list_projection` (`app.services.web_projects`) declares the admin
    project list capabilities with `ui.list_contracts` — searchable name,
    status/type/priority/region filters, name/priority/created sort, pagination —
    and delegates the read to `projects_service.projects.list`
    (`operations.project_lifecycle`), which owns the canonical filtered/sorted
    query; the projection issues no query of its own. Gated by the existing
    granular `project:read`.

17. `ui.referral_list_projection` (`app.services.web_referrals`) owns the admin
    referral filter, stable sort, page/row projection, canonical URL, and KPI
    cohort links. It depends on `ui.list_contracts`, `ui.projection_contracts`,
    and `referrals.program`. The route redirects invalid or clamped request state
    to the owner-provided URL; the template uses shared sortable-header,
    pagination, page-size, and keyboard-visible row-action controls.

Rule: filters and search are applied before pagination; every paginated sort has
a unique tie-breaker. Web list state is encoded in URL query parameters so deep
links, refresh, and browser history reproduce the same projection. A changed
search, filter, sort, or page size starts at page one. Templates render the
owner-provided query and page metadata and do not hand-build competing query
strings, totals, page counts, or sort rules. Under the global Dotmac UI
standard, the interaction model follows the Carbon data-table, filtering, and
pagination patterns, with WCAG 2.2 AA as the accessibility floor. This is a
behavior standard, not a Carbon visual-theme migration. Column-configuration
responses derive their `sortable` flags from the corresponding resource owner
rather than the legacy table-field registry.

## UI Bulk Actions

1. `ui.bulk_action_contracts` owns code-native selection modes and the
   authorized presentation of bulk action label, description, semantic tone,
   preview/confirmation requirements, execution mode, and result-reference
   vocabulary. It does not own business eligibility or mutation.
2. A bulk resource declares page select-all semantics and whether the list owner
   supports an explicit all-filtered selection. Empty selected IDs never imply
   a filtered cohort.
3. `ui.customer_bulk_action_projection` is the first adopted resource. It
   projects only customer actions authorized for the current principal and
   depends on `ui.customer_list_projection` for filtered scope semantics.
4. The customer table header checkbox selects the visible page. A separate
   affordance promotes that selection to all rows matching the canonical search
   and filters. Search, filter, or page-size changes clear the selection.
5. `app.services.web_customer_actions` resolves selected IDs or the explicit
   filtered query again at preview and execution. Mutations require the preview
   count and confirmation token in the confirmation request and fail with HTTP
   409 when the cohort has changed. Customer activation/deactivation binds that
   token to each selected account's observed active state and the requested
   target; customer deletion also binds active/subscription eligibility, so a
   newly eligible row cannot be deleted under a stale impact preview. Commands
   continue to re-check domain state and return partial outcomes or notification
   identifiers.
6. `ui.invoice_bulk_action_projection` adopts the same interaction contract for
   invoice issue, send, void, mark-paid, PDF-generation, and export actions.
   `app.services.web_billing_invoice_bulk` remains the single eligibility and
   command owner; the projection calls that policy rather than copying status
   rules into Jinja or JavaScript.
7. Invoice selection is page-only. Mutation and PDF-generation commands require
   a server preview, exact resolved count, and impact token. The token covers
   selected membership plus each row's eligibility outcome, so a status change
   that expands or shrinks impact after preview fails with HTTP 409. Execution
   re-checks eligibility and audits only processed invoice IDs.
8. `ui.support_ticket_bulk_action_projection` projects authorized support-ticket
   update controls and page-row eligibility. Selection is page-only and never
   implies all filtered tickets.
9. `support.ticket_bulk_commands` requires an in-modal, side-effect-free preview
   of exact selected membership, the shared proposed change set, eligible rows,
   and skipped reasons. Confirmation binds matched count, proposed changes, and
   every row eligibility outcome; drift returns HTTP 409.

Migration record:

- Old owners: customer Jinja/Alpine independently exposed the actions menu,
  stored selected IDs, and interpreted an empty array as every row matching
  submitted filters; the reusable data-grid selectable mode was a second local
  ID collector without action capabilities. Invoice Jinja/Alpine independently
  hardcoded actions and confirmation text, while its full-page and HTMX tables
  rebuilt different filters, rows, and pagination.
- New owners: `app.services.bulk_actions` owns the generic interaction contract,
  `app.services.web_customer_bulk_actions` owns the customer projection,
  `app.services.web_customer_lists` owns filtered customer cohort semantics,
  `app.services.web_billing_overview` owns the invoice list/export scope,
  `app.services.web_billing_invoice_bulk_actions` owns invoice action
  presentation, and existing customer/invoice command services retain mutation
  and consequence ownership.
- Verification: contract, service, route/template architecture, selection,
  explicit filtered-scope, list-query, preview, membership/eligibility drift,
  and partial-outcome tests protect the boundary.
- Cutover gate: no-selection requests fail closed; unauthorized actions and
  selection controls are omitted; page selection and filtered promotion are
  distinguishable; preview membership or eligibility drift prevents execution.
- Fallback retirement: the customer page no longer exposes bulk actions before
  selection, and `resolve_bulk_customer_scope` no longer falls through from an
  empty ID list to filtered execution. The invoice page no longer hardcodes
  action buttons, eligibility assumptions, manual query strings, or a second
  HTMX-only table. Other resources remain unchanged until they adopt named list
  and bulk projections.

Support-ticket bulk migration record:

- Old owners: the public bulk API delegated to `Tickets.bulk_update`, but that
  method directly changed status, priority, and assignment while bypassing the
  canonical single-ticket lifecycle consequences. The admin list had no
  selection, authorization projection, impact preview, or drift contract.
- New owners: `support.ticket_bulk_commands` owns selected membership, change
  normalization, preview, confirmation, and outcomes;
  `ui.support_ticket_bulk_action_projection` owns authorized page-selection
  presentation; `support.ticket_lifecycle` remains the mutation/consequence
  owner through `Tickets.update`.
- Verification: service, projection, route-permission, architecture, template,
  no-selection, preview/no-side-effect, proposal drift, eligibility drift,
  lifecycle-audit, and structured-outcome tests protect the boundary.
- Cutover gate: unauthorized users receive no selection controls; empty or
  filtered scope fails closed; no update executes without the exact server
  preview; changed membership, eligibility, or proposal returns HTTP 409.
- Fallback retirement: `Tickets.bulk_update` no longer writes lifecycle fields
  directly and the admin page exposes no unpreviewed or all-filtered ticket
  mutation path.

Rule: bulk controls appear only when a selection exists and a canonical command
supports it. Filtered, customer-visible, financial, destructive, or fleet-wide
operations require explicit impact preview and confirmation. WCAG 2.2 AA labels,
indeterminate state, selected-count announcements, and focus/keyboard behavior
are part of the contract; hidden controls are never authorization enforcement.
## UI Action Forms

## UI Display Formatting

1. `ui.display_formatting` / `app.services.display_format` owns the code-native
   display rules for normalized currency codes, currency symbols, single-value
   money, ordered multi-currency summaries, configured display timezone, and
   timestamp strings. Missing scalar facts use one explicit em-dash marker;
   only a caller-declared aggregate absence becomes zero.
2. Financial, network, usage, and other domain owners retain the typed facts:
   amount, ISO currency, unit, timestamp, and whether a value is zero, unknown,
   stale, or unavailable. Formatting never changes or derives those facts.
3. Single-currency values may use the declared symbol form. Mixed-currency
   totals use explicit ISO-style codes, group normalized codes independently,
   sort them deterministically, and never add unlike currencies together.
4. `control.settings_spec` owns the configured billing default currency and
   scheduler timezone. `ui.display_formatting` resolves those settings for
   display; templates and mobile clients do not independently default to NGN or
   Africa/Lagos when a projection declares another value.
5. `mobile/lib/src/core/formatters.dart` is the existing platform renderer for
   mobile layout and locale mechanics. It is not a second owner of currency,
   timezone, missing-value, or unit facts.
6. First adoption: billing overview/invoice/aging, payments/import history,
   ledger, and reconciliation delegate their multi-currency summary strings to
   `app.services.display_format`. Their former private currency-code, amount,
   and grouped-total formatter copies are retired.

Migration record:

- Old owners: four billing web projection modules each carried equivalent
  `_currency_code`, `_format_currency_amount`, and `_format_currency_groups`
  implementations. Their behavior could drift independently from the existing
  global money filter and configured display settings.
- New owner: `app.services.display_format`; billing services still assemble
  domain-owned totals and request a display projection from that owner.
- Missing-state correction: the prior scalar `format_money` helper rendered
  missing or invalid values as currency zero. It now renders the shared em-dash
  marker; aggregate callers request zero explicitly through the grouped/amount
  functions.
- Verification phase: formatter behavior tests cover normalization, explicit
  ISO labels, deterministic grouping, duplicate normalized codes, empty totals,
  and setting resolution. Existing billing overview, payment import, ledger,
  and reconciliation tests prove byte-compatible output.
- Cutover gate: the four pilot modules import `display_format` and contain no
  private currency normalization or formatter definitions.
- Fallback retirement: the private formatter copies are removed. Other screens
  migrate incrementally; no second shared formatter or template-local default
  may be introduced.

Rule: formatting projects authoritative facts; it does not repair missing data,
convert currency, select business precision, or collapse unknown into zero.
Callers must make aggregate-zero behavior explicit and keep unlike currencies
separate.

1. `ui.action_form_contracts` owns the code-native interaction projection for
   an action: visibility, disabled reason, semantic tone, impact preview,
   confirmation requirement, declared fields/options, owner-produced hidden
   action evidence, submitted values, and structured field/general errors.
2. Domain command and transition services still own authorization, business
   eligibility, validation, locking, mutation, audit, and consequences. A form
   contract is a read projection, not an execution bypass. The command owner
   rechecks every decision when the form is submitted.
3. Unauthorized actions are omitted. State-ineligible actions are shown
   disabled only when the owner-provided reason helps the operator understand
   what must change.
4. `ui.payment_proof_review_projection` is the first adopted resource.
   `financial.payment_proofs` owns submitted/verified/rejected eligibility,
   duplicate-reference policy, payment creation/allocation, WHT consequences,
   and typed command errors. The web projection adapts those facts into the
   shared verify/reject forms.
5. Failed payment-proof submissions render the same detail page with declared
   values preserved and typed field or general errors. Successful mutations
   keep POST-Redirect-GET. Templates do not map domain error strings back to
   fields or infer review availability from raw status.
6. High-impact actions expose their consequence before submit and require an
   explicit confirmation supplied by the action contract. Web rendering uses
   branding-owned semantic roles and WCAG 2.2 AA labels, descriptions, focus,
   invalid-state, and live-error semantics.

Migration record:

- Old owner: payment-proof detail Jinja selected review actions from raw status,
  declared fields/defaults, hardcoded impact/confirmation copy, and redirected
  failed submissions through one unstructured query-string error.
- New owners: `app.services.payment_proofs` supplies typed eligibility and
  command errors; `app.services.web_billing_payment_proofs` builds the resource
  projection through `app.services.action_forms`; the shared Jinja macro only
  renders that contract.
- Verification phase: contract, domain eligibility, route/RBAC, submitted-value,
  structured-error, template architecture, accessibility, payment, duplicate,
  and WHT tests.
- Cutover gate: the payment-proof template contains no raw verify/reject form,
  status-based action branch, local confirmation copy, or domain-error mapping.
- Fallback retirement: the successful redirect remains; the old failed-action
  redirect is removed. Other forms migrate incrementally only after their
  command owner exposes equivalent eligibility and error contracts.

Rule: UI action projections explain and collect a command; they do not decide or
execute it. Routes pass submissions to the named owner, templates render only
declared controls, and the owner rechecks permission and eligibility under the
same lock or transaction that protects the mutation.

### Payment-arrangement staff safe actions

`financial.payment_arrangements` owns arrangement eligibility, lifecycle and
installment facts. `financial.payment_arrangement_staff_actions` owns the
staff-only approve, cancel and manual-installment confirmation workflow. It
locks the arrangement and schedule, recomputes the owner preview, rejects a
changed fingerprint, stages the owner transition, and stages audit evidence in
one coordinator transaction.

The admin projection renders only owner-available actions through
`ui.action_form_contracts`. The exact installment and collection-shield
consequence are visible before submission. A required labeled checkbox replaces
browser confirmation dialogs. Manual installment recording is described as
external evidence and does not claim to create a billing Payment or ledger
entry.

Migration record:

- Old owners: payment-arrangement routes, web helpers and Jinja status branches
  committed lifecycle changes, selected installment targets and wrote audit
  after the state commit.
- New owner: `financial.payment_arrangement_staff_actions`, consuming locked
  preview and transition participants from `financial.payment_arrangements`.
- Cutover gate: all three staff actions carry explicit confirmation and the
  current deterministic preview fingerprint.
- Fallback retirement: direct admin mutation helpers, raw action forms,
  template-local action eligibility, money formatting and browser confirmation
  JavaScript are removed.

Rule: adapters may explain a typed failure, but may not retry an old
payment-arrangement preview. A changed schedule or lifecycle requires a fresh
owner projection and new operator confirmation.

### Dunning staff safe actions

`financial.dunning` owns case eligibility, lifecycle state, canonical
collectible-receivable checks, action-log/event evidence, account projection,
and every service-access consequence. `financial.dunning_staff_actions` owns
the staff-only pause, resume, and close confirmation workflow.

The staff preview binds one action to explicit selected case IDs and reports
each row as eligible or skipped. Its fingerprint includes case existence,
lifecycle version, current step, resulting state, and close-time collectible
receivables by currency. Confirmation locks cases and accounts in stable order,
recomputes the preview, applies the exact eligible subset, and stages audit in
one transaction. Changed scope or eligibility returns a conflict; any staging
failure rolls back the cohort.

The list uses page-only `ui.bulk_action_contracts` selection and always opens a
server preview before bulk pause/resume. Individual actions live on the detail
page and use `ui.action_form_contracts`. Close remains disabled while canonical
collectible receivables exist. Closing a case does not restore service or clear
financial access locks.

Migration record:

- Old owners: dunning routes, web helpers and Jinja forms selected actions,
  committed each case independently, swallowed bulk exceptions, and wrote
  audit after state commits.
- New owner: `financial.dunning_staff_actions`, consuming locked preview and
  lifecycle participants from `financial.dunning`.
- Cutover gate: every pause, resume, and close confirmation carries exact
  membership, an owner fingerprint, and explicit operator confirmation.
- Fallback retirement: direct row mutation forms/routes, browser dialogs,
  per-case bulk commits, generic exception swallowing, post-commit audit, and
  the web-only direct lifecycle helpers are removed.

Rule: skipped rows are a previewed owner result, not a caught execution
exception. A confirmed dunning cohort is atomic for every eligible row shown.

### Invoice batch and reminder safe actions

`financial.billing_automation` owns the durable billing-run workflow and
postpaid invoice-cycle execution. `financial.prepaid_service_renewals` remains
the independent owner for funded prepaid periods. A confirmed manual invoice
batch disables prepaid renewal so its execution matches its stated and
previewed scope.

For ADR 0007 Phase 2 migration evidence, the postpaid owner's typed preview is
the complete current recurring cycle: base service plus every included
recurring add-on, with exact `SubscriptionAddOn.id`, quantity, proration, route
cap, tax, currency, and component totals. A multiple active add-on price, mixed
currency, missing route, or route-capped quantity is explicit blocker evidence,
not silent parity. The prepaid owner's typed preview remains faithful to its
current base-only renewal and explicitly lists recurring add-ons it excludes.
That cohort cannot pass the Phase 2 gate until the prepaid owner consumes
complete rated obligations under an approved money cutover.

Recurring add-on structural capture follows the same guaranteed owner-output
rule as the sale boundary:

- `billing.addon_contract_backfill` is a temporary shadow-migration observation
  owner. It locks the current contract boundary, captures exact
  `SubscriptionAddOn`, `AddOn`, and unique active recurring `AddOnPrice`
  identities for one complete future service period, binds the snapshot to a
  confirmation fingerprint, and stages
  `billing.addon_contract_backfill.captured` with durable idempotency evidence.
  The thin `billing_target_shadow` operator adapter exposes separate
  `preview-addon-contract` and `capture-addon-contract` commands; capture
  requires both the reviewed fingerprint and a stable idempotency key.
- `billing.contracts` is the only contract-line writer. It receipts that output,
  refuses stale/mismatched contract or sale identity, supersedes the shadow
  version at the period boundary, preserves existing line lineage, records each
  recurring add-on as `component_key == str(SubscriptionAddOn.id)`, and stages
  `billing.contracts.shadow_recorded` in the same transaction. The existing
  obligation and terminal-evidence owners then advance the chain.
- The migration does not detect-and-repair cross-owner drift. Ambiguous prices,
  mixed currency, partial-period terms, missing structural sale anchors,
  invalid quantities, and stale versions fail closed at capture/consumption.
- Authority remains unmoved. The temporary producer is retired only after
  customer purchase/cancellation, admin, route, sales, and remediation writers
  emit the same typed billing-terms output atomically with their source
  transition and a real complete-cohort run passes the documented gate.

The batch review projects exact billable subscription IDs, accounts, periods,
currencies and base charges. Its fingerprint binds that membership to the
normalized cycle/date and optional failed source run. Confirmation recomputes
the preview before launch. `BillingRun` persists `running/success/failed`,
launch kind, staff principal, confirmed fingerprint and retry lineage. Only a
failed run can start a reviewed retry; retry creates a new linked run.

Invoice-list issue/send/mark-paid/PDF actions and AR-aging reminders submit
explicit invoice IDs to `ui.invoice_bulk_action_projection`. The existing
invoice bulk command owner returns eligible/skipped membership and a scope
token; `ui.action_form_contracts` renders the required confirmation. Changed
membership or eligibility fails closed.

Billing execution is an owner-managed, resumable workflow rather than one
database transaction. Per-subscription/period invoice-line keys repair partial
work on retry. `BillingRun` is authoritative operational evidence; the
post-status `AuditEvent` is a rebuildable projection and cannot reverse
already-created invoices.

Migration record:

- Old owners: batch-page JavaScript and raw forms confirmed launches, retry was
  offered for every run state, manual generation implicitly invoked prepaid
  renewal, and AR aging bypassed the required invoice scope token.
- New owners: billing automation supplies dry-run/execution facts and durable
  run state; the batch and invoice bulk projections build exact shared review
  forms.
- Cutover gate: every manual/retry launch carries the current fingerprint,
  actor and explicit confirmation; every invoice bulk action carries exact
  membership evidence.
- Fallback retirement: browser dialogs, direct batch execution, success/running
  retry, JSON-only batch preview, direct issue/send/mark-paid/PDF routes, and
  AR-aging send bypass are removed. The unused `BillingRunSchedule` table,
  shadow `billing.billing_run_schedule_config`, save route, and form are also
  retired because no scheduler consumed their values; `scheduler.registry`
  remains the sole cadence and enablement owner.

Rule: a retry is a new traceable run, never a mutation of history. Manual
invoice generation does not silently invoke another financial workflow.

## UI Semantic Presentation

1. Account, subscription, invoice, payment, outage-incident, support-ticket, and
   work-order lifecycle owners remain authoritative for raw values and
   transitions. `network.device_state` remains authoritative for the derived
   device operational vocabulary, retry-pending state, and alarm classification;
   `network.connection_health` owns the separate customer-safe
   `connected/trouble/outage` verdict and diagnostic wording.
2. `ui.status_presentation` owns the human label, semantic tone (`positive`,
   `info`, `warning`, `negative`, or `neutral`), and non-color icon key for each
   account, subscription, invoice, payment, outage-incident, device operational,
   customer connection health, support-ticket, and field work-order status.
3. Admin customer, billing, and support screens; customer billing/support;
   reseller invoice/ticket and customer-connection screens; network outage and
   device NOC consoles;
   catalog, billing, service-status, support, CRM outage, and network-device API
   projections; customer mobile;
   field job/manager APIs; and field mobile consume the same
   `StatusPresentation` contract.
4. Server responses carry semantic meanings, not Tailwind classes, Flutter
   colors, or other platform-specific tokens. `customer.branding` owns the
   concrete primary, secondary, and five-role semantic palette. Web renders it
   through `/branding/theme.css`; both Flutter clients resolve the same
   `BRAND_SEMANTIC_*_COLOR` build inputs from `brand.json`. Renderers select a
   role and icon; they do not keep local role-to-color dictionaries.
   The runtime stylesheet also owns compatibility aliases for legacy non-neutral
   Tailwind palette names and the ordered `data-1` through `data-7` categorical
   palette used by charts and maps. Structural neutral surfaces, text, borders,
   shadows, white, and black remain owned by the design-system foundation.
5. Unknown or old-backend values fail neutral. Clients may humanize the raw
   value for compatibility, but must not recreate state-specific tone policy.

Migration record:

- Old owners: account label/color dictionaries in customer Jinja and portal
  context, subscription/invoice/ticket state-to-tone switches in customer
  mobile, invoice and ticket label/color dictionaries in portal/admin/reseller
  Jinja, configurable ticket status colors, and work-order label/color
  dictionaries in field mobile, plus outage lifecycle badges in the manual,
  classifier, and notification-review consoles, plus device operational label/
  color maps in NOC inventory, detail, monitoring, worklist, and map surfaces,
  plus customer-connection state/color switches in portal, reseller, and mobile
  diagnostic surfaces.
- Old color owners: literal Tailwind/hex tone maps in the shared badge,
  connection diagnostics, NOC map/summary renderers, and Flutter status widgets.
- New meaning owner: `app.services.status_presentation`, transported through
  `app.schemas.status_presentation.StatusPresentation`. New concrete-color
  owner: `app.services.brand_profiles` and the generated brand theme tokens.
- Compatibility boundary: legacy Tailwind palette names resolve to branding-owned
  scales at runtime; new or touched code uses primary, accent, semantic, or
  categorical data tokens directly. Literal chart, map, and mobile palettes are
  retired from migrated domains.
- Verification: exhaustive enum coverage, API serialization, projection,
  template architecture, and Flutter parsing/rendering tests.
- Cutover gate: no customer account/subscription, invoice, payment, outage-incident,
  device operational, customer connection-health, support-ticket, or field
  work-order status dictionary or local semantic role-to-color map remains in
  migrated templates or mobile presentation paths. Configured semantic seeds
  must retain WCAG 2.2 AA text contrast in light and dark themes.
- Fallback retirement: client compatibility fallbacks are neutral-only and may
  be removed after all supported servers emit `status_presentation`.

Rule: UI consumers render semantic tones and icon keys through branding-owned
theme tokens. They do not decide that a domain state is positive, warning,
negative, informational, or neutral, and they do not assign a literal color to
one of those roles locally.

## Secrets and Credentials

1. Bootstrap secrets required before the application starts use environment or
   mounted secret files.
2. Low-cardinality application and integration secrets use OpenBao references.
3. High-cardinality customer, device, and connector credentials use the
   declared encrypted database-field inventory.
4. Scheduled rotation stages current and previous keys, converges stored
   ciphertext, and retires the previous key only after the grace period.
5. `secrets.settings_migration` is the sole migration boundary for replacing
   noncanonical secret-setting values with OpenBao references. Its operator
   command is dry-run by default and never prints secret values.

Rule: callers request a secret or credential outcome from the owning service.
They do not choose fallback precedence, store plaintext, reveal existing values
in forms, or rotate key material directly.

## Notifications and Communications

1. Notification channel policy owns channel eligibility and preferences.
2. Event notification policy owns event enablement and balance-notification
   suppression.
3. `communications.eligibility` owns the recipient suppression ledger and the
   transactional-versus-marketing send decision.
4. `communications.intents` owns communication intent lifecycle, recipient and
   channel expansion, including authorized subscriber contacts, and
   delivery-outcome projection. Customer bulk-message previews query
   `communications.customer_policy` through its typed, bounded-query cohort
   interface. Execution remains on the established notification/intent owner,
   which rechecks current policy; the admin route does not become a second
   canonical writer. Preview evaluates supported template conditions in bounded
   cohort queries, validates a bounded render sample, shows a bounded recipient
   sample with masked destinations, and returns full impact counts. Confirmation
   is bound to the resolved destinations, template content, variable mapping,
   condition outcome, and suppression decision; drift requires a fresh preview.
5. `communications.customer_experience_intents`
   (`app/services/customer_experience_communications.py`) owns the named
   project/task/field/ticket customer communication requests, their content,
   native relationship lineage, and stable dedupe identities. It requests
   email, direct WhatsApp, and push outcomes through `communications.intents`;
   it does not select recipients, decide suppressions/preferences, or deliver.
6. `communications.ephemeral_actions` owns the allowlisted, typed, non-secret
   action envelope and just-in-time sensitive-message materialization
   orchestration. Calling domains still own capability purpose, claims,
   lifetime, and consequences. The worker must not persist or log rendered
   bearer content or exception text that may contain it.
7. Notification service owns notification rows and delivery lifecycle.
8. `operations.sla_escalation` owns operational SLA policy lifecycle,
   event-scoped escalation planning, and escalation acknowledgement/cancellation.
   Every operational domain emits named facts into this owner. Operators configure
   entity type, event key, escalation level, unresolved delay, channels, active
   state, and applicable severity/impact conditions at
   `/admin/notifications/sla-policies`. Policies cover tickets, work orders,
   outages, projects and project tasks, inbox conversations, provisioning failures, network
   devices/sites, subscribers, payment incidents, and payment proofs. A domain
   service may not embed a fallback SLA duration or channel list. When no active
   policy matches, the owner invents neither a deadline nor an escalation.
9. Staff notification service owns internal/admin notification creation,
   permission-targeted staff audience resolution, and materialization of review
   requests into the assigned admin notification inbox. For payment proofs it
   resolves active system users who effectively hold `billing:proof:verify`
   (including active admin and wildcard grants) and creates one clickable unread
   inbox item per reviewer. It projects those reviewers as the event audience for
   `operations.sla_escalation`; only the active UI policy decides whether and when
   email, WhatsApp, SMS, push, web, Nextcloud Talk, or webhook escalation occurs.
   The financial owner closes the shared request and cancels pending escalation
   when it verifies or rejects the proof. Opening an inbox item is scoped to its
   assigned system user; the target action performs its own domain permission
   check again.
9. `communications.customer_read_state` owns customer notification read/unread
   state and unread counts across the web portal and mobile app. Subscriber
   metadata is its bounded persistence mechanism; `/me/notifications` projects
   that state, and `/me/notifications/read` is the self-scoped mutation
   boundary. Device storage is only a one-way legacy migration input. The
   identity-cleared GET response cache may render last-known state offline but
   never accepts read decisions.
9. The contracted `communications.team_inbox_*` owner family replaces the old
   `communications.team_inbox` catch-all. Observations commit normalized
   provider facts before processing; threads own conversation/message identity;
   contact resolution, routing, operator state, outbound intents, receipts,
   commands, widget commands, projections, repair, realtime, health evidence,
   and campaign materialization each have the exact owner named in the
   generated registry above. `app.web.admin.inbox` translates HTTP inputs and
   owner outcomes; list definitions, filter/sort/page normalization, metrics,
   unread decisions, and action eligibility live in the typed projection
   service. Inbox ORM rows have no writer outside the `team_inbox_*` family —
   campaigns and other domains request materialization rather than constructing
   rows themselves.
   `app.team_inbox_smtp` owns only the dedicated SMTP process lifecycle,
   readiness check, and continuous/deployment probe orchestration; it delegates
   every inbound write and exact-probe verification to
   `team_inbox_smtp_inbound`, delegates consent-gated probe delivery to the
   canonical notification delivery point and email transport, and is never
   started from a web-process lifespan.
   `communications.conversation_ticket_handoff` owns issuing a support ticket
   from a conversation: eligibility, idempotency, and the
   `Ticket.origin_conversation_id` provenance link, of which it is the only
   writer. Ticket identity, state, and official timeline remain owned by
   `support.ticket_lifecycle`, which exposes the provenance as a keyword-only
   argument on its create command so no ticket payload can forge it. One
   conversation may issue many tickets. Issuance never transitions the
   conversation — opening a ticket and resolving a thread are separate
   decisions, and conversation status stays with `communications.team_inbox`.
   Replay is keyed on conversation, actor, and title rather than the transport
   request id, so a resubmitted form replays instead of opening a second ticket.
   `app.web.admin.inbox` remains the only HTTP translator for this owner.
   `communications.team_inbox_contact_resolution` also owns the reviewed
   projection from an existing Inbox route to a canonical Party contact point. It validates the
   point, provider scope, target Party, and active contact relationship against
   `party.registry`, but does not let Party services mutate Inbox routing.
   `communications.team_inbox_projection` owns the
   exact open, Unreplied, Needs Attention, unassigned, muted, snoozed, and
   failed-outbound cohorts. Needs Attention is recomputed from ordered message
   chronology, agent reply provenance and delivery state, conversation
   lifecycle, and ticket handoff provenance. KPI links carry the matching
   server filter; resolved conversations cannot leak into an open-derived
   drilldown.
9. Campaign services own marketing audience, sequence, and content decisions.
   They apply `communications.eligibility` when building an audience, before
   enqueueing a send, and again through the marketing communication intent at
   delivery. Agent replies are transactional communication intents and remain
   eligible unless the suppression ledger blocks all communication. Campaigns
   request a canonical sender key; email delivery alone resolves that key to
   SMTP identity and credentials.

Rule: domain services request a notification outcome; they should not construct
notification rows, choose email/SMS/WhatsApp directly, or maintain recipient
read state outside the owning service. Admin inbox routes must not load or
mutate inbox ORM rows, control commits, or select alternate mutation helpers.
Invoice issue/send actions emit `invoice_sent` once through the invoice owner;
web and bulk adapters do not hand-compose or directly deliver a second email.

## Events and Webhooks

1. Event dispatcher owns event routing.
2. Event-store service owns event rows, handler attempts, retry lookup, cleanup,
   and stale processing.
3. Webhook delivery service owns webhook delivery rows and queueing.
4. Subscription lifecycle event service owns lifecycle audit rows.

Rule: handlers orchestrate. Persistence and retry bookkeeping live in services.

## Observability

1. Observability service owns task/job run recording.
2. Task reliability owns task metadata, heartbeat interpretation, and alerting.
3. `observability.channel_health_contracts`
   (`app.services.channel_health_contracts`) owns monitoring activation, active
   windows, natural-versus-synthetic mode, silence thresholds, severity, and
   runbook declaration for every sensitive external channel. Every supported
   channel has exactly one enabled contract or an explicit disabled reason.
   Invalid or incomplete registries fail closed and alert immediately.
4. Metrics collectors expose read-only gauges/counters for runtime pressure.
5. Scheduled single-flight producers own expensive business-health snapshots;
   metrics collectors only read those bounded snapshots.
6. The cross-Dotmac scrape contract is defined in
   `docs/METRICS_SCRAPE_SAFETY.md`: `/metrics` reads process-local instruments,
   bounded snapshots, and static metadata only. It never opens a database
   session or invokes a business resolver.

Rule: Celery tasks report lifecycle through shared observability helpers; they
should not write heartbeat/run rows directly unless they are the helper.
Scrape-time collectors must never perform unbounded business-table scans or
per-customer financial reconstruction. Database and infrastructure queries are
also produced out of band so pool exhaustion cannot make the scrape path block.
Prometheus and transport adapters consume channel-health contract facts; they
must not hard-code a second activation flag, business window, silence threshold,
or severity. High-volume channels use natural freshness. Low-volume sensitive
channels require a verified end-to-end synthetic signal that cannot be forged
by an external payload marker. Once a contract is enabled its declared alert
consequence is live—there is no shadow decision path.

## Network Domain

Dependency order:

1. `network.identity`: resolves cross-model network/customer links.
2. `network.monitoring_inventory`: owns monitoring inventory, metric records,
   alert rules, and alert state mutations.
3. `network.fiber_source_staging`: owns immutable source manifests, normalized
   staged map facts, and non-authoritative duplicate/match suggestions. Staging
   preserves evidence; it cannot create, merge, retire, or delete canonical
   assets.
4. `network.fiber_topology`: owns fiber asset identity and connectivity, the
   OLT-to-customer topology integrity contract, ordered validated subscription
   traces, bounded fault-candidate ranking, and customer-trace evidence
   completeness. Electronic inventory, telemetry, and imported map geometry are
   observations until this owner validates their identity and edges. Missing or
   ambiguous edges remain explicit gaps; ranking does not declare an incident or
   decide numeric cutover-review readiness. An operational cable must have two
   distinct active, canonically referenced termination points and approved route
   geometry, and its active component must be rooted at an exact serving
   PON/OLT boundary. It also declares positive cable `fiber_count` and exact
   numbered cores through `FiberStrand.segment_id`; cable names cannot establish
   ownership. Supports and poles are mounts, not implicit terminations.
   Revision `361_fiber_plant_operational_integrity` adds the active-row database
   check. `network.fiber_plant_integrity` owns rooted activation, safe cable
   retirement, exact numbered-core materialization, and cable/splitter capacity
   guards; topology remains the trace and diagnostic read owner.
   Its preflight reports legacy violations and never repairs them implicitly.
5. `network.fiber_support_structures`: owns canonical pole/support identity,
   lifecycle, ownership, inspection, and lease state, plus exact reviewed mount
   edges to cabinets, FAT/access points, splice closures, and fiber segments.
   Imported pole rows remain observations. Reviewed source-identity decisions
   may create or link a support through `network.fiber_asset_changes`, whose
   approved support mutations delegate here. Mount preview is write-free;
   confirmed proposals bind exact state, require independent review, and lock
   and revalidate before execution. Geometry, names, external IDs, and
   proximity never create a mount. A support with active mounts cannot retire.

Physical-continuity owner: `network.fiber_physical_continuity` owns reviewed
fiber racks, ODF/patch panels, one-channel connector ports, exact strand-end
terminations, core splices, patch cords, and the ordered physical-core evidence
hash. Every link has preview, independent review, locked execution, and exact
result evidence. Rack-unit, panel-port, cable-core, and splitter capacity remain
explicit and bounded. Duplex patching is two explicit channel links sharing an
assembly label; MPO/MTP inventory fails closed until an exact assembly/lane
model exists. Names, labels, proximity, geometry, legacy `FiberSplice` rows,
and `FiberSegment.fiber_strand_id` never create continuity. Direct legacy splice
writers are retired; historical rows remain readable evidence.

6. `network.fiber_asset_changes`: owns reviewed passive-fiber change requests
   and their approved mutations. Approved support mutations delegate to
   `network.fiber_support_structures`; this generic request owner does not
   construct supports or mount edges. Operational cable decisions delegate exact
   infrastructure-end, PON-rootedness, core-materialization, and safe-retirement
   enforcement to `network.fiber_plant_integrity`. Splitter and splitter-port
   decisions delegate persistence to `network.splitter_inventory`, which is also
   the owner used by API and admin form adapters and rejects declared ratio/count
   conflicts. Rack, panel, and connector changes plus field core-splice review
   delegate to `network.fiber_physical_continuity`; the change-request workflow
   does not write a parallel splice graph. Attachment decisions remain separately
   owned and neither names nor geometry create those edges. Direct map imports
   are not a second writer.
7. `network.fiber_identity_decisions`: owns dual-reviewed source identity
   decisions and canonical source links. Point-asset creates become pending
   `network.fiber_asset_changes` requests; the source link is projected only
   after the approved asset exists.
8. `network.fiber_identity_review`: owns the latest-source review queue,
   immutable batch proposal manifests, exact-manifest independent review
   attestations, bounded execution-run evidence, and idempotent finalization
   sweep. It delegates each decision transition to
   `network.fiber_identity_decisions`; execution and reconciliation never
   approve the resulting asset change request.
9. `network.fiber_field_observations`: owns immutable technician observations
   bound to exact staged feature content, native Sub work orders, technician and
   person identities, explicit labels or canonical references, measurement
   facts, and active same-work-order private attachment pointers. It retains
   contradictory observations and projects agreement, conflict, superseded
   evidence, and drift by verification scope. It cannot infer identity or
   endpoints, create or advance decisions, approve changes, mutate canonical
   topology, or establish a cutover threshold. For an explicitly planned job it
   also enforces the exact source scope owned by
   `network.fiber_field_verification_job_scope`; legacy jobs without a plan keep
   their existing behavior.
10. `network.fiber_field_verification_job_scope`: owns the versioned work-order
   metadata contract for exact planned staged-feature IDs, content hashes, and
   worklist row hashes. A planned job cannot observe a source identity outside
   that scope or content that has changed. Names, labels, geometry, and
   proximity never expand it.
11. `network.fiber_field_verification_worklist`: owns the exhaustive read-only
   latest-source field-evidence worklist, deterministic evidence-gathering
   priority, and exact row/report digests. Every staged point and path remains
   visible, including current agreement. Existing native work-order references
   are context only. This owner cannot create or assign jobs, record
   observations, infer identity or endpoints, generate decisions, mutate
   topology, establish a field threshold, or claim cutover readiness.
12. `network.fiber_field_verification_jobs`: owns bounded, write-free previews
   and confirmed execution of exact staged-source job plans. A plan selects at
   most 100 explicit current worklist rows and binds their IDs, row/content/
   geometry hashes, existing job context, the complete worklist report hash,
   explicit subscriber, schedule, optional technician, and deterministic native
   job identity. Execute re-runs the worklist and exact plan digest, then
   delegates create and optional assignment to
   `operations.work_order_commands` in one transaction and records actor audit
   evidence. It never constructs either work-order table and adds no action to
   the read-only worklist or map.
13. `network.fiber_field_verification_map`: owns the complete read-only exact
   staged-GeoJSON overlay for the field-verification worklist, presentation-only
   geometry classification and bounds, and exact feature/overlay digests. It
   fails closed on worklist/source identity or hash drift, colors features only
   by owner-produced evidence priority, and retains unrenderable source geometry
   without repairing or hiding its cohort row. It cannot snap, transform, infer
   topology, create jobs or observations, mutate state, establish a threshold,
   or claim cutover readiness.
14. `network.fiber_work_order_evidence_map`: owns the read-only exact-GeoJSON
   fiber evidence projection for one explicitly scoped native Sub work order.
   It consumes the immutable field-observation cohort and complete
   field-verification overlay, requires every job observation to map exactly
   once, returns no
   unobserved source feature, strips all other jobs' evidence, and retains
   current versus superseded source context plus exact hashes. Current
   field-verification geometry remains presentation evidence; superseded
   observations do not verify it. This owner cannot create or assign jobs,
   record observations,
   repair geometry, infer or mutate topology, establish a threshold, decide
   customer impact, or claim cutover readiness.
   The `field_mobile` consumer is a read-only projection adapter, not a
   new owner. It opens this exact endpoint from native job detail, renders only
   the returned job cohort and server-owned context/geometry presentations, and
   stores offline snapshots under authenticated-principal scope plus the
   composite `work_order_public_id + report_sha256` evidence identity. A newer
   report replaces the prior snapshot for that principal and job; an offline
   hit is visibly stale, and no cached report can cross a principal or
   work-order boundary. Authoritative 4xx scope, permission, or lineage failures
   never fall back to stale evidence. The client cannot discover unobserved
   assets, aggregate jobs, repair geometry, infer topology/fault/customer
   impact, create observations, or mutate work and topology state.
15. `network.fiber_identity_coverage`: owns exhaustive read-only reconciliation
   of every latest staged cabinet, FAT/access point, splice closure, building,
   and pole/support identity to immutable batch/review/run evidence,
   change-request state, canonical asset state, and exact source provenance. It
   keeps canonical-model support, identity coverage, lifecycle, mount state,
   and field-verification evidence independent. A support identity is terminal
   only when it is applied with current provenance or explicitly reviewed and
   rejected. Identity coverage does not infer or decide support mounts. Field
   observations remain visible but do not alter this component owner's gates.
   The approved numeric policy consumes them only through
   `network.fiber_cutover_readiness`. Passing component gates provide evidence
   for that separate combined review only; this owner cannot infer identity, create
   or advance decisions, approve change requests, mutate assets, or authorize
   production cutover.
16. `network.fiber_connectivity_decisions`: owns reviewed staged-path endpoint
   decisions, shared typed termination resolution, canonical segment source
   provenance, and connectivity reconciliation. Geometry never supplies an
   endpoint. A canonical edge exists only after two explicit endpoint
   references and their segment mutation are independently reviewed and
   applied through `network.fiber_asset_changes`. Direct termination/segment
   API mutations are retired; read endpoints remain projections.
17. `network.fiber_connectivity_review`: owns immutable operator-scale staged-path
   proposal manifests, exact-manifest independent all-or-nothing attestations,
   and bounded execution/reconciliation evidence. Every create or link row binds
   the exact staged content hash and operator-supplied canonical endpoint IDs;
   geometry is evidence only and never selects an endpoint. It delegates every
   decision transition to `network.fiber_connectivity_decisions` and never
   approves the resulting termination or segment request owned by
   `network.fiber_asset_changes`.
18. `network.fiber_connectivity_coverage`: owns exhaustive read-only
   reconciliation of every latest staged path to immutable batch/review/run
   evidence, decision lifecycle, termination/segment request state, and canonical
   segment source provenance. It keeps exact, unassigned, superseded,
   overlapping, and blocked source coverage separate from pending, applied,
   rejected, declined, stale, failed, and evidence-drift lifecycle state. Field
   observations are projected separately and do not alter this component
   owner's gates. Its conservative gates produce evidence for the numeric
   cutover-readiness owner only. It never infers endpoints, creates or advances decisions,
   approves change requests, mutates topology, or authorizes production cutover.
19. `network.fiber_cutover_readiness`: owns policy
   `fiber_topology_cutover_v1`, the complete global cohort evidence projection,
   and the sole combined numeric topology cutover-review readiness decision.
   It consumes exact identity/connectivity coverage, the exhaustive field
   worklist, canonical topology blockers, and exhaustive active-customer traces
   in one repeatable read-only snapshot. Gates require 100% exact-current and
   current terminal evidence, 100% traceability, 100% current agreement for
   required field rows, and zero blockers. Explicit dormant low-risk rows would
   require a 20% audit with a 25-row minimum; any discrepancy blocks, and above
   2% expands that asset class to complete review. No authoritative dormant
   classifier exists, so all staged rows remain required. Missing POP/OLT,
   splitter, and customer-endpoint field contracts fail closed. A passing report
   is independent-review evidence only and cannot authorize or perform a
   production cutover.
20. `network.ont_topology_observations`: owns durable allowlisted network facts
   about an ONT's exact electronic location. UISP supplies an exact ONT, parent
   OLT, and numeric PON observation; it may initialize missing PON inventory.
   Huawei F/S/P observations can link only an already-modeled exact active PON.
   The owner may initialize empty ONT OLT/PON edges with source evidence, but it
   never overwrites or merges an existing identity edge. Missing or conflicting
   data remains unresolved observation evidence in the admin review queue and
   cannot itself authorize an assignment repair. Inferred repair, assignment
   form reads, Huawei authorization adapters, and PON metadata forms cannot
   bypass the owner to merge, create, reactivate, or rewrite PON inventory or
   references.
21. `network.ont_assignment_commands`: owns normal explicit ONT service
   assignment, normal release, verified PON-move projection, and exact audit
   results. It requires exact ONT, subscription, and modeled PON identifiers;
   derives the subscriber only through the subscription bridge; and fails
   closed when an existing customer, subscription, PON, or OLT identity
   disagrees. MAC, name, address, work-order, registration, and geometry
   inference cannot select identity. UFiber MAC matching is preview-only,
   management IPAM cannot manufacture an assignment, and generic CRUD adapters
   delegate or retire mutation.
22. `network.ont_assignment_identity`: owns preview, independent review,
   execution, and exact-result evidence for exceptional ONT assignment
   identity repair. Repairs bind one active assignment, exact subscription,
   PON, OLT, and the complete set of active ONT/subscription conflicts. The
   subscriber projection comes only from the exact subscription. Subscriber,
   address, name, geometry, and imported registration inference are forbidden.
   Public assignment mutations and registration-driven writes are retired;
   changed execution inputs close without mutation. The admin review queue is a
   thin projection: it detects disagreements, requires exact identifiers,
   derives OLT only from the exact modeled PON, enumerates conflicts
   deterministically, and requires preview before proposal. It never promotes a
   detected discrepancy directly into a decision or mutation.
23. `network.ont_assignment_cutover`: owns the exhaustive read-only audit of
   active assignment invariants, stable exact blocker evidence, and the future
   database-constraint readiness gate. It scans all active assignments before
   display filtering, keeps required identity, active-ONT uniqueness,
   active-subscription uniqueness, and exact active network targets visibly
   distinct, and routes investigation to `network.ont_assignment_identity`.
   It never chooses replacement identity, creates a proposal, mutates an
   assignment, or enables a constraint. A clean report is necessary but does
   not itself authorize cutover.
24. `network.ont_assignment_cutover_batches`: owns immutable operator-selected
   cleanup manifests and their independent review attestations. Every manifest
   binds the complete cutover report SHA-256, each selected finding SHA-256,
   and explicit per-assignment action, target, and complete conflict IDs. It
   atomically delegates proposal and review state to
   `network.ont_assignment_identity`; it cannot execute a batch or mutate an
   assignment. Approval only makes the individual decisions eligible for their
   identity owner's locked revalidation and execution.
25. `network.ont_assignment_cutover_verification`: owns immutable
   post-execution verification attestations. It copies every terminal identity
   decision's exact result payload/hash, binds those results to a fresh
   exhaustive assignment audit, and keeps pending, applied, stale-closed,
   conflict-closed, declined, batch-scope residual, and global blocker evidence
   distinct. A verifier must be independent of proposal, review, and execution
   actors. Pending decisions cannot be attested. This owner cannot execute a
   repair, mutate an assignment, or enable a constraint.
26. `network.ont_assignment_cutover_coverage`: owns the read-only reconciliation
   of every current assignment cleanup finding against all immutable proposal,
   review, decision-result, and verification lineage. One repeatable snapshot
   distinguishes exact, superseded, unassigned, and overlapping coverage while
   keeping decision outcome, current repair-scope state, and verification drift
   separate. Its conservative gates produce evidence for a separate constraint
   authorization review; they do not authorize or enable constraints, and this
   owner cannot execute repairs or mutate assignments.
27. `network.ont_assignment_constraint_authorization`: owns immutable requests
   and independent approve/decline attestations for a future assignment
   constraint cutover. Each request binds an explicitly named target, expiry,
   complete clean coverage payload, current coverage hash, and independent audit
   hash. Approval fails closed on expiry or current evidence drift. Current,
   stale, expired, declined, and invalid state is derived rather than maintained
   as a second mutable lifecycle. Even current approval is only evidence for a
   separate reviewed DDL change; this owner has no constraint or DDL executor.
28. `network.ont_inventory_release`: owns the local electronic-identity release
   consequence of an explicit return-to-inventory transition. After successful
   external OLT/ACS cleanup it locks the ONT and all assignments, closes active
   assignments, clears exact subscription/subscriber/service-address and PON
   references, and clears ONT OLT/PON/F/S/P identity in one transaction. It
   chooses no replacement identity. Legacy SmartOLT import is preview-only and
   bulk provisioning migration cannot target a PON.
29. `network.fiber_access_attachments`: owns preview, independent review,
   execution, and audit evidence for exact PON-to-splitter-input and
   ONT-to-splitter-output attachments plus exact directed
   splitter-output-to-downstream-input cascades. It is the only writer for
   active `PonPortSplitterLink` and `SplitterCascadeLink` records and the ONT
   splitter projection. It requires exact ONT/PON/OLT agreement, one rooted
   acyclic splitter tree, directed active ports, root-first cascade construction,
   leaf-first removal, one-to-one port occupancy, and explicit insertion loss
   for every cascaded splitter stage. Geometry, cabinets, names, ratios,
   proximity, and legacy splitter assignments never create an edge; stale
   execution closes without mutation.
30. `network.access_path`: resolves `subscriber/subscription -> access path`
   from identity plus validated fiber topology. Its fiber end-to-end projection
   composes customer/ONT, exact passive cables, reviewed racks/ODFs/patch cords,
   numbered in-use cores and core splices, OLT identity, authoritative
   provisioning NAS, and the observation-agreeing forwarding chain to a
   core/border root. It emits typed gaps and one combined evidence hash. Live
   RADIUS NAS remains a separate observation and never supplies a missing
   authoritative hop.
31. `network.radius_sessions`: resolves online-now state and active-session NAS
   observation evidence from authoritative active-session facts. It does not
   decide which session is primary for a customer-facing use case.
32. `network.ont_runtime_status`: owns Huawei bulk ONT status observations, the
   Huawei OLT pollability predicate, and admission of those poll tasks. Scheduled
   sweeps and stale inventory reads request the same retry-safe infrastructure
   observation poll through this owner. These bulk reads are not tracked device
   commands; operator-requested single-ONT refresh remains operation-backed.
33. `network.device_state`: derives NOC operational state, retry state, and alarm
   classification from administrative intent and monitoring observations, and
   owns the `up/degraded/down/maintenance` vocabulary. Retry-pending gaps stay
   binary but are non-alarming; presentation renders retry-pending `down` as
   warning/clock rather than a confirmed negative failure.
34. `network.ont_status_refresh`: owns admission of stale ONT runtime-status
   refresh requests from read surfaces. ONT inventory may request a refresh when
   displayed evidence is stale, but it must not poll OLTs directly. Huawei ONTs
   request the `network.ont_runtime_status` infrastructure observation poll with
   per-OLT cooldown/admission; UISP-managed ONTs remain refreshed by the UISP
   topology sync source. `Status refresh pending` means the displayed value is
   retained or derived and needs asynchronous confirmation, not that the page
   performed a live check.
35. `network.outage_impact`: resolves affected customers from topology.
36. `network.device_groups`: owns device-group mutations, membership, and bulk
   action queueing.
37. `network.outage_lifecycle`: owns the persisted incident status vocabulary,
   incident transitions, and typed lifecycle output emission
   (`outage.created`/`outage.confirmed`/… staged atomically with each
   transition, plus the legacy `network.alert` webhook fan-out). The
   registered outage lifecycle projection handler consumes those committed
   outputs to attach operational owners/watchers and plan or cancel SLA
   escalations through `operations.sla_escalation`; a consequence that
   cannot be applied stays a failed retryable delivery. Outage resolution
   emits recovery evidence only — it never closes support Tickets or
   WorkOrders (see `docs/designs/NETWORK_OUTAGE_RESPONSE_LIFECYCLE.md`).
38. `network.connection_health`: combines authoritative path, live-session,
   last-mile, impact, and active-incident inputs into the customer-safe
   `connected/trouble/outage` verdict plus headline/message/advice. It does not
   own device operational state or raw online-session observations.
39. `network.control_plane_intent`: owns the shared desired-state delivery
   lifecycle, control-plane target/revision identity, and vendor status
   projections. Vendor adapters project through this one
   desired-to-readback lifecycle.
40. `network.huawei_cli_response`: owns Huawei CLI response classification,
   stable error codes, expected-absence predicates, unsupported-command
   detection, and idempotent response semantics. Huawei SSH sessions, protocol
   adapters, readback verification, and web workflows consume these projections
   and do not maintain firmware response string tables. A response classified
   as accepted is transport evidence, not proof of convergence; write workflows
   still require the control-plane intent readback contract. Protocol adapter,
   authorization, provisioning, and reconcile history persist the sanitized
   classifier projection as operation evidence; raw CLI output is not retained.
41. `network.routeros_sot`: owns typed MikroTik desired state, the managed
   resource/field registry, Dotmac ownership markers, verified reconciliation,
   and periodic drift evidence. Router routes and tasks only orchestrate it,
   and it projects through `network.control_plane_intent`.
42. `network.forwarding_topology`: owns reviewed downstream-to-upstream
   forwarding declarations and the official operational graph for exact device,
   interface, site, core/border/NAS role, VRF, preference, configuration intent,
   and, where applicable, peer, route, next-hop, and NAS termination identity.
   Declare and retire transitions require a write-free preview, exact hash
   confirmation, independent review, locked revalidation, audit evidence, and
   an exact hashed result. `network.control_plane_intent` and
   `network.routeros_sot` remain configuration owners; this owner never applies
   device configuration. LLDP, BGP, routing-table, and RADIUS data remain
   observations. LLDP must agree on both exact interfaces, border paths require
   exact current BGP and route observations, NAS paths require exact LLDP and
   route observations, and RADIUS session counts remain online context only.
   Missing, expired, conflicting, or invalid evidence fails closed. Customer
   upstream paths, reachability ancestry, outage localization, and blast radius
   consume only reviewed declarations with current required observation
   agreement. No observation, legacy `NetworkDevice.role`, imported identifier,
   name, or inferred site can create official forwarding path.
   `app.services.network.forwarding_observation_collector` is the read-only
   RouterOS adapter: it uses GET requests scoped by active reviewed declarations,
   requires exact router/device, interface, and VRF identity, and submits
   expiring facts only through the forwarding owner. Its scheduled task is
   fail-closed behind `network.forwarding_observation_collection`; enabling the
   control starts an observation shadow run and does not authorize declaration,
   configuration, customer/outage cutover, or any router write.
   `network.access_path.resolve_fiber_end_to_end_path` is the read-only composed
   proof across this graph and `network.fiber_topology`. It requires the exact
   subscription/ONT, passive segment inventory, and one exact reviewed physical
   connector/patch/core/splice route, one OLT identity node,
   the authoritative provisioning NAS on the selected agreeing declaration
   chain, and a core/border root. It preserves typed gaps and one combined
   evidence hash. Live RADIUS NAS identity remains a separate observation and
   cannot fill a missing provisioning or declaration edge. Production remains
   blocked until complete reviewed passive/declaration cohorts and fresh
   observations pass their documented cutover gates.
43. `network.operation_ledger`: owns the tracked device operation lifecycle and
   status vocabulary, the terminal-transition guard, correlation-key duplicate
   suppression, stale-active reclamation, parent/child rollup, and whether an
   operation may run, resume, or be re-executed. Celery is transport: tasks
   report progress through the ledger and do not decide retry eligibility.
   `app.services.task_reliability` declares each task's retry/idempotency/
   visibility contract and is a *projection* of this owner, not a second
   authority. A contract may only claim operator redrive
   (`MANUAL_REDRIVE`/`ADMIN_REDRIVE`) once a redrive path exists in the ledger;
   declaring an affordance that does not exist is drift, not policy. Recovery
   requests require a reviewed current-state head, scoped idempotency key,
   operator reason, retry limit, and a typed handler. The failed operation is
   immutable; each approved attempt is a separate `redrive_of` operation.
   `app.services.network_operation_recovery` is the ledger's typed recovery
   boundary. It cannot dispatch task names or payloads supplied by a route.
   The initial recovery handler covers operator-requested, observation-only
   single-ONT status refresh. Firmware, configuration, lifecycle, and other
   device writes remain ineligible until their owning service provides
   current-state validation and replay safety.
44. `network.operation_dispatch`: owns transactional staging and transport for
   operation-backed network commands. The operation and its exact versioned
   command are committed together in `network_operation_dispatches`; request
   handlers never commit an operation and then publish its device task. The
   scheduled publisher is the only broker writer for registered commands, and
   every broker message enters a typed envelope that atomically claims the row
   before device code runs. Duplicate envelopes therefore do not duplicate a
   device command. Broker acceptance, worker acknowledgement, completed
   delivery, exhausted publication, and reconciliation-needed execution are
   transport evidence, not substitutes for operation/device outcome. Unknown
   execution fails closed and requires current-state review before redrive.
   The cutover covers operator-requested single-ONT status refresh, ONT
   authorization and baseline repair, TR-069 bootstrap verification attempts,
   ONT and OLT firmware entry commands, and OLT-triggered ONT desired-state
   reconciliation. Recurring or stale-read-triggered bulk OLT status collection
   is observation polling owned by `network.ont_runtime_status`, not an
   operation-backed command. Firmware verification/readback continuations retain
   their own state machines and are not parallel command-origination paths.
45. `network.tr069_commands`: owns typed TR-069 CPE command admission,
   execution claims, and outcome classification. Admission atomically commits
   the operation, encrypted execution payload, redacted operator projection,
   lifecycle event, and typed dispatch. The optional
   `network.tr069_command_admission` capability affects only new work;
   dispatch and reconciliation are permanent responsibilities. GenieACS task
   responses are observations: acceptance becomes pending, a recorded fault
   becomes failed, absence after acceptance becomes succeeded, and any
   ambiguous or interrupted delivery becomes `unverified`. Bulk actions fan
   out through this same owner. The old CRUD/execute service, bulk Celery
   envelope, execution flag alias, and runtime legacy adoption path are
   removed; migration `408` terminalizes all pre-cutover executable rows and
   clears their payloads.
46. `network.ont_provisioning_commands`: owns acceptance and duplicate handling
   for ONT authorization, baseline repair, and bootstrap verification commands.
   It commits each operation and typed dispatch atomically. Admin, API, and bulk
   callers receive operation/dispatch identifiers and never publish the device
   task themselves.
47. `network.ont_provisioning_execution`: owns the tracked authorization,
   baseline-repair, DB-only baseline preview, bootstrap retry, parent rollup,
   and bulk-item transitions.
   Celery workers claim an existing dispatch and delegate execution here; they
   do not create operations or decide a parallel retry policy. Delayed bootstrap
   attempts are separate immutable dispatch rows on the same child operation,
   while Inform-driven completion uses the same parent projection.
48. `network.ip_assignment_lifecycle`
   (`app/services/ip_assignment_lifecycle.py`): owns the exact
   `IPAssignment.subscription_id` bridge and reviewed exact-service IPv4 ledger
   repair during the shadowing migration. The ownership preview still permits
   only the high-confidence missing-link cohort. The lifecycle preview may keep
   or create the desired exact assignment, link a same-subscriber legacy row,
   deactivate an explicitly reviewed stale exact-service cohort, or release an
   exact terminal-service cohort. Confirmation requires the exact identifiers,
   preview SHA-256, actor, reason, and idempotency key; locks and recomputes all
   evidence; and fails closed on cross-customer or cross-service ownership,
   incomplete deactivation, reserved or management addresses, inactive pools,
   and routed-block hosts. The separate served-projection preview requires one
   exact active assignment plus aligned RADIUS and session observations. Its
   owner command may change only `Subscription.ipv4_address`, then stages a
   durable event that asks `access.radius_projection` to rebuild the exact login
   and `access.session_enforcement` to disconnect only sessions still framed
   with the old address. Normal provisioning and admin assignment writers
   remain explicit migration debt until the runtime cutover described in
   `docs/designs/IP_ASSIGNMENT_LIFECYCLE_SOT.md`.
48a. `network.cpe_dialer_credential`
   (`app/services/cpe_dialer_credential_reconcile.py`): owns the derived CPE
   PPPoE dialer credential projection and its fingerprint comparison and
   readback. `AccessCredential`/`RadiusUser` remains the authoritative
   subscriber access credential, and `access.radius_projection` remains the
   only writer of the RADIUS auth tables — this owner never writes RADIUS.
   What the CPE dials with
   (`OntUnit.desired_config` `wan.pppoe_username` / `wan.pppoe_password`)
   is a DERIVED projection of that credential, and this reconciler is its
   single canonical writer. Operator-typed dialer values are converged back
   onto the authoritative credential; they never flow the other way, and the
   ONT configuration UI must not claim that editing them repairs
   authentication. Delivery to the physical CPE stays with the ONT reconciler
   (`app/services/network/reconcile`), which diffs desired against the
   ACS-observed username and pushes over TR-069. Comparison is by keyed
   HMAC-SHA256 fingerprint over the `(username, secret)` pair; credential
   values are never logged, returned, or stored in a drift record. Only the
   username is readable back from a CPE, so convergence is proven as
   projection readback (recorded fingerprint) plus device readback (last
   ACS-observed username). `pppoe_health.CATEGORY_CREDENTIAL_MISMATCH` remains
   the read-side detector; this owner is its repair path.
   Boundary change: this promotes the CPE dialer value from an
   independently operator-written field to a derived projection with one
   writer. The previous de facto writer —
   `web_network_ont_actions.config_setters.set_pppoe_credentials` — remains
   available as a manual CPE repair action and is documented as
   non-authoritative; its values are re-converged by this reconciler.
49. `network.ip_pool_utilization` (`app/services/ip_pool_utilization_snapshot.py`):
   owns IP-pool utilization reads — the daily utilization snapshots and the
   live per-pool used/total counts consumed by the network report. The live
   count (assignment-join basis) is deliberately distinct from the snapshot's
   CIDR-capacity basis; both definitions live in this owner and are documented
   in its docstrings. Web layers compose these reads; they do not count
   addresses or assignments themselves.

Provisioning dispatch authority migration: the retired path published Celery
tasks from admin/API/bulk callers and created the operation inside the worker.
The new command owner creates the operation and typed dispatch atomically before
publication. The cutover gate is: migration `294` applied, the dispatch
publisher enabled, and workers running code that claims dispatch envelopes.
For broker-retention safety, an old envelope without a dispatch identifier may
only re-submit its intent to `network.ont_provisioning_commands`; it cannot enter
device code. Remove this compatibility adapter after one maximum broker-retention
window has elapsed after production cutover. The old direct-publish and
worker-owned-operation paths have no fallback authority and must not return.

ACS device identity: the GenieACS `_id` (`OUI-ProductClass-Serial`) of a CPE is
owned by the TR-069 Inform handler and persisted on
`Tr069CpeDevice.genieacs_device_id`. No planner, applier, task, or adapter may
construct one from a default OUI or ProductClass — the fleet spans several ONT
models and a fabricated identifier is a permanent NBI 404 that retries forever.
The ONT reconciler resolves the identifier from that record, or from the `_id`
the ACS itself reported for the serial on the same pass, and fails closed
otherwise: a device absent from the ACS, an ambiguous multi-document match, or a
recorded-versus-reported disagreement produces an OLT-only plan plus an explicit
`ont_not_informing` / `acs_identity_unresolved` wait, never a speculative push.
Repeated undeliverable passes are counted on the ONT and escalated so a
permanently broken ONT cannot fail silently in the sweep.

Rule: pollers and map collectors write observations; `network.fiber_topology`
validates passive asset identity and connectivity;
`network.forwarding_topology` owns official forwarding declarations and
agreement; resolver services decide state; event services decide consequences.
Customer-facing outage, SLA, expiry suppression, support
context, and escalation should consume these network SOT layers.
Outage list/detail projections add `StatusPresentation` from the raw lifecycle
state; templates and CRM consumers do not maintain their own state-to-severity
dictionaries. Device operational state and customer connection-health verdicts
remain separate vocabularies owned by their corresponding network services.
Numeric fiber cutover-review readiness is decided only by
`network.fiber_cutover_readiness`; component reports and UIs cannot maintain a
parallel threshold.
Customer portal, reseller, support context, API, and mobile verdict surfaces
consume the same connection-health payload and semantic presentation; raw
session dots on subscription views remain observation surfaces outside that
verdict.

## Subscriber Sessions

Dependency order:

1. `sessions.radius_reconciliation`: is the canonical writer of the
   `radius_active_sessions` projection; it reconciles external `radacct` open
   sessions and prunes dead rows.
2. `sessions.radius_resolution`: owns customer/subscriber online-now and
   primary-NAS-session resolution over the active-session observations.
3. `sessions.enforcement`: owns CoA, disconnect, and session refresh outcomes
   after billing/access/FUP state changes.

Rule: accounting imports write session facts; resolvers answer online state;
enforcement applies network-side consequences. Billing/access code should not
query `RadiusAccountingSession` or `radius_active_sessions` directly to decide
access.

## Application Sessions

Dependency order:

1. `app_sessions.store`: owns Redis-backed storage, principal indexes, fallback
   store, and revocation epochs.
2. `app_sessions.customer_portal`: owns customer portal session lifecycle,
   refresh, revoke-all, impersonation, and read-only policy.
3. `app_sessions.auth`: owns database auth-session listing and revocation.

Rule: routes authenticate and authorize, but session lifecycle and revocation
policy belongs in session services. Do not duplicate cookie/session mutation
logic in route handlers.

The read-only admin control-plane projection may aggregate database-session
counts and Redis health, but it does not enumerate Redis keys or become a
session writer.

## Runtime Infrastructure

Dependency order:

1. `runtime.realtime_projection` (`app.services.realtime_platform`) owns the
   versioned real-time envelope, Redis topic naming, best-effort publication,
   and the shared WebSocket/SSE reconnect contract. It projects only
   already-committed domain state. Redis pub/sub is at-most-once and has no
   replay; clients refetch canonical read models on connect, reconnect, or a
   `realtime.reset` event. `app.services.realtime_subscriptions` authorizes
   client-selected conversation and operation topics, while the workqueue
   scope owner derives workqueue topics server-side. WebSocket and SSE handlers
   are transport adapters, never domain decision owners. The complete contract
   is `docs/REALTIME_PLATFORM.md`.
2. `runtime.db_sessions`: owns background DB session lifecycle and advisory lock
   safety.
3. `runtime.task_idempotency`: owns duplicate suppression and stale task
   execution rows.
4. `runtime.task_heartbeat`: owns task success/skip heartbeat signals.
5. `runtime.infrastructure_polling`: owns shared native reachability observations
   and the generic network-device pollability predicate. Domain-specific
   collectors such as Huawei ONT runtime status depend on these polling
   mechanics while owning their own observation and eligibility contracts.
   Its polling and topology-warming adapters consume reserved `monitoring`
   queue capacity; bulk ingestion and independently bounded per-OLT MAC harvest
   tasks cannot occupy that worker. Queue placement is transport isolation and
   does not transfer observation or device-state decision ownership.
6. `runtime.infrastructure_health`: owns dependency health checks for
   Postgres, Redis, VictoriaMetrics, Celery, and related infrastructure.

Rule: real-time transports project state only; durable cross-team consumption
uses the event store/outbox. Tasks should use shared DB-session, lock,
idempotency, and heartbeat helpers. Infrastructure pollers write observations
only; network/device SOT services interpret state for customer impact, alerts,
and SLA.

## Provisioning Operations

Dependency order:

1. `operations.provisioning_context`: composes subscriber, subscription, ONT,
   CPE, TR-069, ACS, service address, and NAS context.
2. `operations.provisioning_workflow`: executes service-order workflows and
   provisioning steps from the resolved context.
3. `operations.work_order_status`: declares persisted work-order values and the
   canonical open, assignable, and terminal sets.
4. `operations.work_order_commands`: owns native work-order creation and header
   commands, the native `work_order.project_id`, optional
   `work_order.project_task_id`, and internal-only `work_order.origin_ticket_id`
   bindings, the default-enabled
   `requires_as_built_evidence` policy, assignment decisions/projection, and
   assignment-queue transitions.
   Dispatch API/web and field-manager handlers are authorization/transport
   adapters around this owner. Assignment preview is read-only; execution locks
   the work order, atomically updates the queue and assignee projection, records
   exact previous/result actor audit evidence, and treats an equivalent retry as
   a replay. Direct header assignment fields and direct field-execution status
   changes are rejected. `work_order.project_task_id` is the execution-side FK:
   one project task may require zero or many field visits. The command validates
   subscriber/project consistency and makes established bindings immutable.
   Retained CRM ids are provenance only. Native project/task-binding and evidence-policy
   rejections use transport-neutral `WorkOrderCommandError`; only
   `app.errors` maps them to HTTP responses.
5. `operations.work_orders`: exposes work-order read models and customer links.
   The `work_order` table is Sub's authoritative work-order storage
   (WORK_ORDER_IDENTITY_SOT): identity is the Sub-generated `public_id`;
   `crm_work_order_id` is nullable historical provenance on the `work_order`
   root only — NULL for native rows and never used as a join key. The eleven
   field-evidence tables join solely through the `work_order.id` FK. The CRM
   work-order pull, webhook, sync state, reconcile tasks, and customer mirror
   reads are retired; there is no fallback writer or read path.

   Native mutations delegate to `operations.work_order_commands`. Read-only
   cross-domain worklists and project/task detail projections may filter on the
   authoritative native `project_id` and `project_task_id` bindings to show job
   context, but cannot write work-order or assignment state themselves.
6. `operations.field_completion`: owns field-job completion eligibility, evidence
   requirements, and completion transitions. For work issued from a support
   ticket it requests an atomic outcome projection from
   `support.ticket_work_order_handoff`; that projection records evidence but
   never resolves or closes the ticket.
7. `operations.material_dependencies`: owns the material need and approval that
   can block a Sub service work order, then idempotently projects the configured
   backoffice system's authoritative issue/refusal outcome back into that
   workflow. It never posts stock or selects backoffice inventory. Backoffice
   unavailability never reverses a valid Sub approval. After the per-flow
   cutover, the old local issue/fulfil actions fail closed. The integration
   boundary is `docs/BACKOFFICE_INTEGRATION_BOUNDARY.md`.
8. `operations.project_lifecycle`: owns native project field/status mutations,
   project SLA synchronization, and lifecycle event/notification requests.
8b. `operations.installation_scope` has two entry points onto one root. A sold
   installation is scoped by `ensure_for_project` (subscriber-bound, triggered
   by `sales.fulfillment`). Network buildout is scoped by `ensure_for_buildout`
   (subscriber-less, rooted on a `BuildoutProject`), which mints the native
   project root through `operations.project_lifecycle` with
   `external_system='buildout'` and the buildout id as `external_reference` —
   reusing `uq_projects_external_system_reference` as the idempotency key
   rather than adding a column. Both land on `InstallationProject`, so every
   downstream vendor decision runs one path. Plant work has no subscriber and
   never invents one.

9. `operations.vendor_project_lifecycle` (`app.services.vendor_portal_operations`)
   is the only writer for vendor and staff work transitions on
   `installation_projects`: staff intake `draft -> open_for_bidding` (publish)
   and `draft -> assigned` (direct assignment), vendor
   `approved -> in_progress -> completed`, staff
   `completed -> verified`, and staff rework `completed -> in_progress`.
   Publication requires a bidding window that closes after it opens, because
   the marketplace read and the quote-creation policy both refuse a project
   without one — a project no vendor can quote is not published. Direct
   assignment names the vendor instead of opening a window; the vendor still
   quotes and staff still approve, so assignment decides *who may quote* and
   never what the work is worth. The two intakes are alternatives, not a
   sequence. `installation_project_lifecycle_events.vendor_id` is NULL only for
   an intake decision taken before a vendor exists; every transition from
   `assigned`/`approved` onward carries its vendor. It locks
   the project, rechecks the assigned vendor and current state, and atomically
   appends `installation_project_lifecycle_events` evidence carrying the
   authenticated actor type/id, transition time, previous/result state, vendor,
   optional review/rework reason, and durable event id. The same transaction
   stages typed outbox events `vendor_project.started`, `vendor_project.completed`,
   `vendor_project.verified`, or `vendor_project.rework_requested`. Cross-team consumers
   may read that timeline or consume those events; they do not infer actor/time
   from `updated_at` and do not write project status directly. Vendor routes,
   confirmation handlers, templates, and future delivery integrations are thin
   adapters around this owner. Project verification is an operational decision:
   invoice approval and ERP payment observations do not gate it and are not
   modified by it. The owner raises transport-neutral
   `VendorPortalOperationError` rejections; the application HTTP error handler
   alone maps them to responses. An architecture test prevents this owner from
   importing FastAPI or Starlette. The same named owner also owns the
   installation-project quote and as-built evidence lifecycles, including the
   read-only impact snapshot used before submit; one implementation module is
   therefore declared under one owner name. It likewise owns `quote creation
   eligibility`: which vendor may open a *new* quote on an installation
   project. A vendor may quote work directed at them (`assigned_vendor_id`
   matches) or work genuinely published for bidding (`open_for_bidding` or
   `quoted`, inside a populated `bidding_open_at`/`bidding_close_at` window);
   a project assigned to another vendor, never published, or already awarded
   is not quotable. Awarding closes quoting — post-award change is a variation,
   not another bid. Returning a vendor's own already-open editable quote is a
   read of a row they own and is deliberately not gated by this policy. The
   marketplace listing applies the same shape as a query filter, but the
   listing is a projection: the command owner enforces the decision under lock
   and never infers visibility from what a read happened to return. The vendor project-detail map reads
   proposed and prior as-built geometry through
   `app.services.vendor_routes_api.build_project_route_geojson`; its capture
   controls render only from the owner's `as_built_action` projection and
   serialize the existing `VendorAsBuiltCreate.geojson` contract rather than
   writing route evidence from the template. The same owner controls as-built
   submission versions and staff review transitions from `submitted` or
   `under_review` to `accepted` or `rejected`. Each decision updates the current
   review projection and atomically appends `as_built_route_review_events`
   evidence plus `vendor_as_built.accepted` or `vendor_as_built.rejected`.
   Rejection requires a reason. An evidence decision never implicitly verifies
   or reworks the project, approves an invoice, or infers ERP payment.

   Accepting as-built evidence is also what makes it a record of the network.
   `network.as_built_plant_projection` owns that one derived thing — the
   `FiberSegment` an accepted as-built represents and the
   `as_built_routes.fiber_segment_id` link — and stages it inside the accepting
   transaction, so the fiber map cannot lag an acceptance already committed.
   The evidence stays authoritative here, so a lost segment rebuilds from the
   accepted rows alone; the projection is never the only copy of the truth.
   It creates cable **inactive**: `fiber_segments` requires bound, distinct
   endpoints on an active row, which is the schema stating that unconnected
   cable is not operational plant. Binding endpoints and activating is
   `network.fiber_topology`'s decision — a vendor drawing a line does not
   decide what it splices into. The projection never retires plant, never binds
   endpoints, and never deactivates a segment topology has since activated.
   Cable type comes from the accepted line items and is left unset when the
   vendor's wording is unrecognised, because a wrong cable type in the plant
   record is worse than a missing one.
   Proposed-route review follows the same separation:
   `operations.vendor_project_workspace` owns accept/reject eligibility and the
   exact impact snapshot, while `operations.vendor_project_records` owns the
   locked status change and append-only
   `proposed_route_revision_review_events` evidence. It stages
   `vendor_route_revision.accepted` or `vendor_route_revision.rejected`; neither
   decision approves a quote or changes installation-project state.
   For `completed -> verified`, this owner consumes—but never writes—the active
   linked work orders' `requires_as_built_evidence` policy. The policy defaults
   to enabled, including when no active work order is linked; any active linked
   work order requiring evidence means the latest project as-built submission
   must be `accepted`. A newer pending or rejected submission supersedes an
   older accepted submission for this decision. Verification stores the exact
   work-order policy rows and accepted-evidence identity in the append-only
   lifecycle event `decision_context` and typed outbox payload. The optional
   vendor-supplied `work_order_ref` remains observational and is never used to
   decide verification eligibility.
9b. `operations.vendor_material_release` owns the decision to release
   Dotmac-owned material to a vendor for a project, and the projection of the
   configured provider's issue or refusal back into that workflow.
   `field_material_requests` is work-order scoped with a `TechnicianProfile`
   requester, so it models an employee on a customer job; a contractor drawing
   our cable for a buildout is the other case and needs its own anchor. Release
   is available only to the assigned vendor on approved or in-progress work —
   releasing stock for work nobody has agreed to, or that is already verified,
   releases it for nothing. Sub never posts stock and never selects a
   warehouse. Per `docs/BACKOFFICE_INTEGRATION_BOUNDARY.md`, a provider refusal
   is recorded as an observation and never reverses a committed Sub approval.

9c. `operations.vendor_advances` owns whether Dotmac advances money to a vendor
   and how much. **The amount is entered, not derived** — staff approval is the
   control, and an approver sees the amount, the quote total, and what the
   project has already committed. Sub applies two limits only. The hard bound
   is the approved quote total: that is arithmetic rather than policy, because
   you cannot advance more than the work is agreed to be worth, and cost
   escalation is answered by a variation rather than an over-advance. It counts
   already-committed advances, so the bound cannot be evaded by splitting a
   request. On top of that, `projects.vendor_advance_max_percent` is an
   optional operator guard rail that defaults to no cap and can only lower the
   bound, never raise it — a misconfigured 100% cannot authorise advancing more
   than the quote. There is deliberately no code-default percentage: a number
   nobody chose is policy by accident, and an invented cap forces the
   workarounds this domain exists to eliminate. A rejected or canceled request
   reserves nothing. An
   advance is available once work is approved and before it is verified —
   verified work is complete and invoiceable, which is payment rather than an
   advance. The payables provider owns the payment and any netting against the
   vendor's later invoice: a `settled` advance is an observation of that
   provider, never a Sub decision. Sub never computes settlement, never marks
   itself paid, and never adjusts an invoice total.

10. `operations.vendor_purchase_invoices` owns vendor purchase-invoice state,
   financial totals, submit eligibility, and the financial impact snapshot.
   The configured payables system owns accounts-payable settlement. The current
   Dotmac ERP adapter's dedicated
   `GET /api/v1/sync/sub/purchase-invoices/{source_invoice_id}` contract returns
   the organization-scoped supplier-invoice status and reconciled amounts.
   `integration.dotmac_erp_payables_adapter` is the only current writer for
   Sub's timestamped local observation for that provider. It validates source
   identity, provider identity, currency, and amount reconciliation, rechecks
   the link under lock, and retains the last good observation on failure. The
   vendor read owner
   renders payment only from that observed state through `StateValue`; the ERP
   creation response is preserved separately as the payables-document creation
   status, never proves paid or unpaid state,
   and cannot overwrite the refreshed status during replay. Stale or unavailable
   observations remain visibly distinct.
11. `operations.vendor_submission_confirmation` (implemented by
   `app.services.vendor_submission_proposals`) owns the short-lived signed
   confirmation proposal, stale-preview comparison, idempotency reservation,
   and replay result for lifecycle actions, quote, as-built, and
   purchase-invoice submissions.
   The proposal carries no decision authority: each domain owner locks and
   rechecks its current facts, and the mutation plus idempotency result commit
   once. Vendor web routes only request preview or confirmation.
12. `operations.vendor_project_review_confirmation` (implemented by
   `app.services.vendor_project_review_proposals`) owns signed staff review
   proposals, stale-preview comparison, and exact-replay idempotency for verify
   and rework commands. It carries no project decision policy: it asks
   `operations.vendor_project_lifecycle` for both preview and lock-time
   revalidation, then records the confirmation result in the same transaction.
   Admin routes and templates are thin adapters and require `inventory:write`.
13. `operations.vendor_as_built_review_confirmation` (implemented by
   `app.services.vendor_as_built_review_proposals`) owns the signed, stale-safe,
   exactly idempotent confirmation around an as-built accept/reject decision.
   The proposal is not decision authority: lock-time eligibility and the
   transition remain with `operations.vendor_project_lifecycle`. Staff actions
   require `inventory:write`; vendors receive the resulting status and review
   reason through the project projection.
14. `operations.vendor_route_review_confirmation` (implemented by
   `app.services.vendor_route_review_proposals`) owns the signed, stale-safe,
   exactly idempotent confirmation around a proposed-route accept/reject
   decision. It carries no quote or project decision policy. It rechecks the
   workspace-owned preview under lock, records one immutable review result, and
   requires `inventory:write` at the staff adapter.

Rule: provisioning callers should resolve customer/network context once through
the operations context service before running workflow steps. Step executors may
consume context, but should not rediscover subscriber/ONT/CPE links themselves.
`Projects.update` is the canonical writer for native project mutations;
Kanban, Gantt, normal edit, API, and web adapters delegate to it rather than
maintaining parallel SLA/event/notification paths. Customer and reseller read
authority is the native read-only `customer.experience_lifecycle` composition.
The project mirror, project read flip, work-order mirror control, and Dotmac CRM
project/work-order observation operations are retired. The CRM connector cannot
read project, work-order, work-order-note, or technician-location authority.
Field job detail projects typed native project/task/origin-ticket
context and `completion_requirements`
from the same transition service that validates completion. Field clients consume
that contract and may offer advisory quality checks, but must not invent a separate
completion gate from local checklist state or cached settings.
Work-order API projections carry server-owned status labels, tones, and icons;
field clients retain the raw value for transitions and filtering, but do not
reinterpret its presentation.

## Support Control Plane

1. `support.ticket_lifecycle` owns ticket number allocation, lifecycle, assignment,
    comments, satisfaction, and both signed-link and authenticated resolution
    confirmation/dispute.
2. `support.ticket_configuration` owns the operator-managed priority and ticket-
   type SLA targets shown at `/admin/system/ticket-settings`. Ticket types have
   no fixed code default: zero or no override falls through to the configured
   priority target.
3. `support.ticket_sla_clock` owns ticket SLA clocks and breach facts. A breach
   emits `ticket.sla_breached` to `operations.sla_escalation`; only its active UI
   policy selects the escalation delay, level, audience channels, and conditions.
4. `support.ticket_work_order_handoff` owns the explicit boundary from a
   triaged incident to field execution. A ticket must have a subscriber and an
   active assigned service team; only an active member of that team, holding
   both ticket-update and dispatch-write permission at the adapter, may issue a
   work order. Each idempotency key identifies one issuance, and a ticket may
   issue zero or many work orders. An issuance may be scoped to a
   ticket-linked project task, which writes `work_order.project_task_id` and
   infers its project. `work_order.origin_ticket_id` is the only ticket-to-work
   native link; `Ticket.metadata.work_order_id` and native uses of
   `WorkOrder.crm_ticket_id` are retired. Imported CRM values remain external
   provenance. Migration `406_support_ticket_work_order_provenance` backfills
   exact native links from preserved Ticket CRM provenance, verifies ambiguity
   and subscriber alignment, and retains the external value. `field_visit`
   remains a descriptive tag and has no decision authority.

   Work-order creation and execution remain owned by
   `operations.work_order_commands` and `operations.field_completion`. A
   completed or unable-to-complete field event atomically adds an internal
   system fact to the originating ticket timeline. Support must verify that
   evidence and decide the incident lifecycle; work-order completion never
   silently resolves or closes the ticket.

   When support requests customer confirmation, the communication intent carries
   the native ticket identity and dedupe key. The authenticated portal/mobile
   actions and signed public link converge on the same active confirmation
   capability and the same ticket transition/audit owner.

Rule: support routes and jobs translate requests and delegate ticket decisions
to `app.services.support`. Events and notifications are consequences requested
by that owner, not alternate ticket writers. Ticket/work-order adapters delegate
handoff decisions to `app.services.ticket_work_order_handoff`; tags, templates,
automation rules, and integration transports cannot issue work orders. SLA
durations and escalation channels must not be embedded in support code.

## Customer Data Completeness

1. `customer.data_completeness` (`app.services.subscriber_data_completeness`)
   owns the declared answer to "is this subscriber's data good enough for X?":
   the purpose → required-field policy (`ncc_filing`, `kyc`), the derivation of
   what is missing, the capture backlog (`queue`), and the pre-filing readiness
   counts (`readiness`). One declarative policy — callers ask it rather than
   re-deciding what "complete" means.

Rule: completeness is **derived, never stored**. It asks the same resolver the
consuming report asks (state completeness reuses
`ncc_subscriber_report.infer_state`), so a subscriber cannot be complete here
and Unknown in the return.

Rule: the module is **read-only**. Capture flows through the subscriber owner;
this owner reports gaps and never fills them.

Rule: **suggestions are never auto-applied.** A suggestion is unconfirmed
evidence carrying its source, offered to a human who decides. Reporting a
subscriber we cannot locate as though we know where they are is the
fabrication removed from the NCC return (unresolved state was filed as
"Abuja"); a suggestion that silently became a stored fact would reintroduce it
one layer up. A suggester must also use a signal the presence check does not
already exhaust, or it is dead code by construction.

The registry declares this read-only policy as `customer.data_completeness`.
The portal prompt consumes it through `customer.location_capture`; filing
readiness consumes it directly. Neither caller may turn a derived gap or
suggestion into a stored fact.

## AI Control Plane

AI is advisory: it observes, derives, and recommends; it never decides domain
state (`docs/designs/AI_SOT.md`).

1. `ai.gateway` owns LLM provider calls, redaction, prompt-injection defence,
   and provider/latency/token telemetry. It is a **transport**, like a
   payment or SMS provider — it holds no business rule and owns no domain
   state. Credentials resolve through `secrets` (OpenBao), never settings
   rows.
2. `ai.personas` owns candidate-insight derivation: each persona builds
   bounded context from the owning domain's read surface and returns a
   title, summary, structured output, recommendations, and confidence.
   Personas read; they never write.
3. `ai.insights` (`app.services.ai_operations`) is the canonical writer of
   `AIInsight` rows and owns insight lifecycle — create, acknowledge,
   expire. Generated insights land here and nowhere else.
4. `ai.intake` (`AiIntakeConfig`) owns the per-scope, per-channel decision to
   run AI at all: enablement, confidence threshold, clarification limits,
   and escalation timing. This is AI's only decision.
5. `ai.generation` (`app.services.ai.engine`) owns the bounded on-demand report
   advisory path: advisor lookup, prompt assembly, and token budget. It accepts
   a caller-owned report projection, does not query domain models, and persists
   only by delegating to `ai.insights`. The default-off `ai.generation` control
   gates the admin report surface.

Rule: an insight never mutates domain state. Acting on a recommendation means
calling the domain's declared owner (`support.ticket_lifecycle`,
`operations.work_orders`, `operations.project_lifecycle`,
`communications.team_inbox_commands`), which applies its own guards, events,
and audit.
No module under `app/services/ai*` may construct or session-write a non-AI ORM
row; `tests/architecture/test_ai_boundaries.py` enforces it.

## Control Planes

Feature controls:

1. `control.module_manager`: owns product module enablement.
2. `control.domain_settings`: owns stored setting mutation.
3. `control.settings_spec`: owns setting schema, coercion, and defaults;
   environment values seed stored settings at bootstrap.
4. `control.settings_bootstrap`: materializes startup defaults and notification
   templates through `control.domain_settings`; it does not own runtime policy.
5. `control.feature_registry`: composes module and canonical feature decisions,
   keeps safety gates separate, and validates canonical override requests.
6. `control.effective_state`: is the read-only admin projection implemented by
   `app/services/web_control_plane.py`. It reports the decision and provenance
   from each owner; it is never a mutation path or a second policy resolver.

Decision-input ownership:

| Input class | Named owner / resolver | Canonical source |
| --- | --- | --- |
| Capability and module gates | `control.feature_registry` / `control.module_manager` | canonical module setting plus the registered default |
| Global business and operational tuning | `control.settings_spec` | active database setting, otherwise the registered default |
| Task cadence and task enablement | `scheduler.registry` | scheduler registry and `ScheduledTask` state |
| Per-customer, subscriber, service, or device policy | the named domain owner | the owning domain model or policy record |
| External integration targets | the named integration/configuration resolver | its configuration model; deployment-only endpoints may use a declared environment resolver |
| Credentials and secret material | the named credential resolver | OpenBao reference or an approved local secret pointer |
| Protocol constants and safety invariants | the named domain owner | code, schema, or database constraint |

Settings are inputs to a decision owner; they are not decision owners. Every
important decision has one named owner, and every variable input has one
declared source or resolver. Business and operational tuning must not be
hardcoded at callers. Protocol constants, mathematical constants, enum values,
and safety invariants remain code or constraints unless operators genuinely
need to tune them.

Runtime settings are database-authoritative. `control.settings_spec` resolves
Redis cache, then the active database row, then the registered default. A
`SettingSpec.env_var` is bootstrap and migration metadata only: startup seeding
or the explicit one-way settings-sync command may materialize it into the
database, but runtime resolvers must not treat it as a live override. An
emergency environment override is allowed only when it is registered as a
separate control with visible provenance, an explicit safe failure direction,
and an audited retirement plan.

Rule: optional capability gates should call the feature registry. Callers should
not separately read env vars, domain settings, module state, and legacy flags.
The module manager is the canonical writer UX for registered feature controls:
`Inherit` deactivates the canonical row, while `On` and `Off` persist an explicit
`modules.<canonical_key>` override through `control.domain_settings`. The page
shows stored and effective state separately because an owner module can mask a
feature. Every canonical feature change is audited. Michael approved immediate
alias cutoff on 2026-07-15: registered controls resolve only from an active
canonical modules row or their registry default. Migration 284 materializes
legacy database decisions before deleting retired rows; environment-only values
must be materialized before deployment because a database migration cannot see
deployment configuration. The operational gate and rollback are documented in
`docs/runbooks/legacy-feature-alias-retirement.md`. Retired settings forms, API
fields, seeds, specs, and direct consumers must not recreate a parallel writer.
The customer-financial lifecycle is not a registered capability: its invoice,
renewal, collections, restoration, notification, event, and recovery owners are
permanent under ADR 0003. `Subscriber.billing_enabled` remains an account
activation-admission fact owned by `customer.billing_approval`; revocation
disables non-terminal service through `access.subscription_lifecycle` and may
not leave active unbilled service. Registered capability gates cover optional
transports and
integrations such as RADIUS/session operations, usage/FUP emission, CRM/native
transition work, and GIS/network workers. Numeric intervals, thresholds,
profile IDs, account lists, and other tuning values remain in `settings_spec`.

Decision-input migrations are coherent, domain-scoped ownership changes, not
global literal replacement. Each migration names the old source and new
resolver, proves precedence and
provenance, migrates the highest-risk callers, and removes or gates the old
path. External projections follow the separate authority-MOVE procedure with
shadow verification before cutover.

Authorization:

1. `auth.rbac_catalog`: is the only application and seed writer for roles,
   permissions, and role-permission policy. Catalog identities are normalized
   lowercase identifiers with database-enforced case/whitespace uniqueness.
   Assigned identities cannot be renamed or deactivated, and non-assignable
   permissions are protected admin policy.
2. `auth.subscriber_assignments`: is the only application and seed writer for
   `subscriber_roles` and `subscriber_permissions`. Public commands own the
   grant, audit, event, and cache-invalidation boundary; reseller onboarding
   and seed workflows use flush-only owner collaborators. Role grants are
   global or explicitly scoped to one region/reseller, while direct permissions
   must reference active UI-assignable catalog entries.
3. `auth.permission_gate`: owns request/route permission dependencies.
4. `auth.system_user_assignments`: is the only application writer for
   `system_user_roles` and `system_user_permissions`. Local and ERP HR role
   sources converge independently, managed grants are read-only in local
   administration, and every admin-role removal or deactivation locks the
   canonical admin role before enforcing the final-active-admin invariant.
5. `auth.token_signing`: owns configured JWT key/algorithm resolution and the
   cryptographic envelope for typed capability tokens. Calling domains own
   purpose, claims, duration, and consequences.
6. `auth.staff_provisioning`: coordinates ERP HR and administrative staff
   lifecycle commands and
   is the canonical writer for `SystemUser` identity plus initial local
   credential bootstrap. Each write runs in one verified coordinator
   transaction with assignment-owner managed grants, audit evidence, session
   revocation, and the versioned outbox event. Provisioning events contain a
   user UUID and email digest, never the email or a bearer token. The
   `StaffInviteHandler` creates one communication intent per event; the worker
   revalidates the exact active principal and mints the short-lived password
   capability immediately before transport.
7. `auth.reseller_onboarding`: coordinates administrative reseller record and
   portal-principal creation. Canonical reseller/subscriber initialization,
   credential bootstrap, reseller link, assignment-owner grants, audit, and
   versioned events commit atomically. Its event consequence persists only the
   exact principal identifiers and an email digest; delivery revalidates that
   binding before minting the short-lived reset capability in memory. The
   legacy subscriber-backed mode remains an explicit feature-gated principal
   representation, not a parallel transaction or delivery path.
8. `auth.credential_recovery`: owns public and exact-principal password recovery
   request policy, purpose-bound reset claims and lifetime, durable delivery
   intent, and the credential transition. Request events and notifications
   persist identifiers, an email digest, and safe redirect context but never an
   email body or bearer. Delivery revalidates the exact active local principal
   and mints the bearer only in memory at transport time. Redemption locks the
   principal and credential and atomically replaces the password, spends the
   capability, revokes database sessions, and stages PII-safe audit and event
   evidence. The completion-event projection handler is the one idempotent repair
   path for auth-cache invalidation and customer/reseller portal-session
   revocation. API and web adapters own transport error mapping.
9. `auth.customer_credential_enrollment`: owns purpose-bound local credential
   enrollment for referral-created customer accounts and the atomic
   Subscriber-email verification consequence. It creates no placeholder
   credential and owns no Party or subscription lifecycle state. It submits a
   non-secret action to `communications.ephemeral_actions`; token issuance and
   email rendering occur only at the worker transport boundary. Password,
   capability-lifetime, and request-rate policy resolve through
   `control.settings_spec`; the request/credential/audit/event transaction is
   owner-managed, and completion-event replay is the only authentication-cache
   repair path.

Rule: routes declare permissions and business services receive an authorized
principal. RBAC mutation stays inside RBAC services. Staff-sync, reseller admin,
and credential-recovery adapters carry the authorized actor and applicable
scope as command evidence and never write principals, credentials, roles,
sessions, audit rows, events, or notifications.
Every literal route permission must exist in the seed catalogue; the
architecture parity test makes an absent, therefore ungrantable, permission a
build failure. The effective-state projection reads roles and grants only.

Scheduler:

1. `scheduler.registry`: owns effective task registration, cadence, toggle
   synchronization, and exclusion of event-driven transports from periodic
   registration.
2. `scheduler.operations`: owns `ScheduledTask` CRUD, event-driven transport
   schedule rejection, and manual enqueue.
3. `scheduler.worker_control`: owns worker restart targets/actions.

Rule: task cadence and enablement flow through scheduler config. Optional
capabilities resolve by canonical key through the feature control plane; every
other mutable scheduler boolean has a registered, database-authoritative
`SettingSpec`. Ad-hoc environment/database/default boolean fallback is
forbidden. Permanent lifecycle and projection-repair tasks have no enablement
control and cannot be disabled, renamed, or deleted. Event-driven transports
remain requestable but cannot become independent periodic repair owners. Task
bodies execute work and report status. The effective-state projection reads
`ScheduledTask` state and run timestamps; it never changes cadence, enablement,
or dispatch state.

Network access:

1. `financial.access_resolution`: is the single read-only owner of billable
   service classification, prepaid funding eligibility, and desired RADIUS
   access outcomes. The duplicate `access.control_resolution` registry alias
   and the parallel `customer_service_state` implementation are retired.
2. `access.event_policy`: resolves typed event-driven RADIUS and FUP policy from
   `control.settings_spec` plus validated usage-exhausted action evidence. It
   defines no parallel defaults; incomplete throttle configuration fails
   visibly. Invoice-overdue events remain observations whose consequences are
   owned by financial dunning.
3. `access.walled_garden_policy`: resolves persisted restriction intent to the
   effective hard-reject/captive tier. Hard reject is default; captive requires
   explicit eligible residential opt-in and network readiness.
4. `access.radius_state`: maps the effective tier to RADIUS groups/profiles.
5. `access.radius_reject`: owns reject IP lifecycle.
6. `access.radius_target_registry`: owns external RADIUS database target
   selection, per-target capabilities and schema names, environment bootstrap,
   and cutover-shadow verification. Active `RadiusSyncJob` + encrypted
   `ConnectorConfig` rows are the runtime authority; the environment DSN is
   bootstrap and verification input only, never a runtime fallback.
7. `access.radius_projection`: is the single idempotent writer that projects
   desired access and reject state into `radcheck`/`radreply`/`radusergroup`
   (and the `radcheck_admin`/`radreply_admin` device-login tables), under a
   per-target Postgres advisory lock across every target selected by
   `access.radius_target_registry`. Blocked/suspended users get a walled-garden
   `radreply` rather than row deletion, so suspension takes effect at the BNG
   without losing the captive pay-page treatment.
   It also owns the placement of per-login concurrency policy:
   `Simultaneous-Use` is a FreeRADIUS check/control attribute in `radcheck`, not
   a NAS reply attribute in `radreply`. The database setting
   `radius.simultaneous_use_enforcement_enabled` is the cutover gate and
   defaults off until stale `radacct` ghosts and genuine shared credentials
   have been reviewed. Once enabled, the permanent drift detector identifies
   missing/stale check rows and misplaced reply rows and requests this same
   writer to rebuild them. `radacct` is observed session evidence only; it does
   not own the customer, service, credential, or concurrency decision.
8. `access.session_enforcement`: applies CoA/disconnect outcomes.

Rule: billing, FUP, and admin actions resolve the desired access outcome once,
map it to RADIUS state once, and let enforcement apply the network-side change.
No module outside `access.radius_projection` writes `radcheck`, `radreply`, or
`radusergroup`;
event-time and per-user callers request a projection (full sweep or a scoped
reconcile) or enqueue `refresh_radius_from_subs`. The permanent account-access
reconciler is the only periodic drift detector; the full refresh transport is
never independently scheduled. Target failures are reported per target and
suppress downstream CoA. The closed boundary is pinned by
`tests/architecture/test_radius_projection_ownership.py`.

RADIUS schema names and target capabilities are configuration owned by each
`ConnectorConfig`; access-group names, priorities, address-list names, and
enforcement reconciler thresholds and the simultaneous-session cutover gate are
database settings. Code defaults are bootstrap values only, not parallel
runtime policy.

Service intent:

1. `service_intent.catalog_policy`: owns catalog policy lookup.
2. `service_intent.catalog_validation`: owns catalog consistency checks.
3. `service_intent.catalog_billing_governance`: owns billing-critical catalog
   mutation safety, audit, and operator alerts. Live pricing/cadence is versioned
   rather than edited in place, and routes require `catalog:billing_write`.
4. `service_intent.subscription_lifecycle`: owns the current/proposed lifecycle
   projection, command eligibility, reviewed-head contract, and billing/access
   impact preview.
5. `service_intent.subscription_lifecycle_execution`: owns serialized,
   idempotent execution and structured single/batch outcomes. It delegates the
   resulting mutations to account lifecycle, catalog, billing, scheduler, and
   RADIUS owners. Admin routes and bulk adapters submit commands to this owner;
   they do not update subscription status or offers directly. Admin subscription
   creation first stages the record as `pending`; selecting Active, Suspended,
   Disabled, or Canceled applies the corresponding post-create lifecycle command.
   Disabled is a reversible administrative pause: billing and network access stop,
   while credentials, IP assignments, add-ons, and service configuration remain.
   Restore returns that same service to Active and shifts its next billing date
   by the recorded pause duration, preventing catch-up billing for the disabled
   period. Canceled is terminal and releases or ends those operational service
   resources while retaining audit history.
6. `service_intent.subscription_nas_assignment`: owns commercial-service NAS
   assignment.
7. `service_intent.subscription_billing_cadence`: owns the subscription's
   contracted billing cadence. Cadence is captured on the sales-order line,
   materialized on the subscription at creation, and read by the recurring
   biller (`subscription.billing_cycle` -> offer/version price -> monthly). The
   offer price cadence is fallback-only; catalog offer-cadence immutability
   stays with `service_intent.catalog_billing_governance`.
8. `service_intent.ont`: projects provisioning intent to ONT operations.

Rule: catalog policy and subscription owners define commercial intent. Every
lifecycle execution carries a reviewed head and idempotency key. Network owners
project configured intent without a parallel catalog-to-network adapter.

Integrations:

The implemented contract is `docs/designs/INTEGRATION_PLATFORM_SOT.md`. The
live owners are:

1. `integration.registry`: owns deterministic deployed manifests, manifest
   validation, and current connector capability metadata.
2. `integration.installations`: solely owns version-pinned installation
   lifecycle, immutable config revisions, secret references, and capability
   bindings for platform-managed connectors.
3. `integration.runtime`: solely owns runner selection, version-pinned
   operation envelopes, deadlines, and bounded secret materialization. Runners
   have no Sub database session and cannot decide a domain consequence.
4. `integration.delivery`: solely owns outbound HTTP event subscription,
   delivery identity, retry, dead-letter, and replay evidence.
5. `integration.inbox`: solely owns verified CRM, WhatsApp, and payment provider
   receipt identity, deduplication, and consequence-attempt evidence. Provider
   routes verify before receipt; domain owners decide every consequence.
6. `integration.jobs`: owns targets, capability-bound jobs, pinned runs, and
   their operator lifecycle. Active jobs cannot use string adapter/action
   transport selection.
7. `integration.sync`: owns sync orchestration and checkpoints. CRM observation
   jobs execute only through their enabled `dotmac.crm` capability binding.
8. `integration.backoffice_adapter`: is Sub's local anti-corruption port for
   inventory, workforce, expense, procurement, and payables collaboration. It
   resolves the default enabled versioned capability binding; domain owners do
   not select or import `dotmac.erp`, Zoho, or another provider connector.
9. `integration.erp_material_support`: maps an approved Sub material need to
   the versioned backoffice contract, assigns the stable idempotency key, and
   observes or reconciles provider outcomes. The current connector is
   `dotmac.erp`; replacing it changes the binding and connector, not Sub's
   service-workflow owner or provider-neutral fields.
10. `events.store` remains the domain-event fact owner,
   `scheduler.registry` remains cadence owner, and `secrets.reference_store`
   remains secret resolution owner.

The control plane separates deployed connector definitions, configured
installations, capability grants, runtime execution, inbox, delivery, and
sync/checkpoint responsibilities. Connector definitions are deployed and
approved artifacts; the admin UI does not install arbitrary executable code.

Authority cutover is complete for the platform-managed first-party paths:

| Concern | Retired owner/path | Live owner/path | Cutover state |
| --- | --- | --- | --- |
| Connector catalogue | File discovery and static catalogue projections | Manifest-based `integration.registry` | Complete; runtime registration requires a valid manifest |
| Installation configuration | Provider environment settings and provider-specific credential columns | `integration.installations` with immutable config revisions and secret references | Complete for CRM, ERP, WhatsApp, payments, and outbound HTTP webhooks |
| Sync dispatch | String adapter/action selection | Capability-bound `integration.sync` through `integration.runtime` | Complete; active jobs require a binding |
| CRM | Direct client construction and CRM-specific webhook delivery rows | `dotmac.crm` typed capabilities and `integration.inbox` | Complete for platform transport; ADR 0006 temporarily assigns portal live-chat transport and operational inbox authority to CRM through `crm.chat_session.v1` until the final CRM-exit gate |
| Outbound webhooks and hooks | `events.webhook_deliveries`, endpoint tables, and `integration.hooks` | `integration.delivery` consuming `events.store` | Complete; duplicate models, routes, tasks, and CLI hooks are removed |
| WhatsApp messaging | Settings-backed provider transport | Direct Meta typed messaging capabilities plus `integration.inbox` | Complete; no Twilio or fallback transport |
| Backoffice/ERP | Direct provider transport clients | Default enabled typed backoffice capability binding (currently `dotmac.erp`) | Complete; the connector remains observation/transport only and is replaceable without changing Sub domain owners |
| Payments | Direct Paystack/Flutterwave services and payment-specific webhook dead letters | Typed payment capabilities plus `integration.inbox` | Complete; billing owners alone decide financial state |

Migration `380_integration_platform_cutover` removes the retired tables,
columns, settings, and enums and has no downgrade path. Disabling or correcting
the current binding is the recovery mechanism; retired transports are not a
fallback.

The CRM ticket-observation cutover is explicit and fail-closed. The
installation owner adds and connection-validates
`crm.ticket_observation.v1`; the jobs owner binds and activates the reviewed
manual `Pull CRM Tickets` job; the scheduler owner supplies cadence. An enabled
`crm.ticket_pull` control is executable only when exactly one enabled binding
and one active job agree. Deployment, scheduler, and webhook adapters reject
the incomplete state rather than generating an unbound task loop.

Rule: integration routes and webhooks validate and enqueue. Connectors translate
bounded, typed contracts; they never write Sub domain tables or decide payment,
subscriber, access, ticket, work-order, network-intent, communications, or
official-timeline state. Domain owners produce outbound projections and decide
inbound consequences. The effective-state projection derives health from run,
delivery, backlog, authentication, and circuit facts and reads OpenBao metadata
without reading secret values; installed or enabled never implies healthy.

Sub remains complete when a backoffice provider is unavailable. A valid Sub
decision commits independently and records failed collaboration for retry or
reconciliation. Sub never queries a provider database or stores a cross-system
foreign key. Each system owns its local identifiers, including tax identifiers;
contracts carry source-scoped correlation references only where collaboration
requires them. The integration platform is local to Sub, not an enterprise-wide
control plane or shared identifier registry.

## VPN / Remote Access

Dependency order:

1. `vpn.key_material`: owns WireGuard keypair generation and private-key
   at-rest encryption.
2. `vpn.system_interface`: owns the VPS-local WireGuard interface state and the
   projection of desired peers onto the running interface.
3. `vpn.wireguard`: owns WireGuard server and peer lifecycle and the peer config
   and MikroTik RouterOS script generation.
4. `vpn.routing_readiness`: resolves whether a VPN interface is ready for device
   access.

Rule: admin VPN routes and device-access callers resolve server/peer lifecycle,
config and RouterOS script generation, key material, and interface readiness
through these owners. `web_vpn_*` adapters and device-access code do not build
WireGuard config, mutate peers, or write the system interface directly. The
Redis `vpn_cache` is a rebuildable projection of server/peer configs, never a
source of truth.

## Geospatial

1. `gis.geocoding`: owns address and coordinate resolution, geocode lookup, and
   result caching.
2. `gis.spatial_sync`: owns GIS/spatial data synchronization and spatial feature
   import and projection.

Rule: address/coordinate resolution and spatial data synchronization resolve
through these owners. API, web, and task callers request a geocode or a sync
outcome; they do not embed their own geocode lookups or spatial write logic.

## Sales and Referrals

1. `sales.orders`: owns sales order lifecycle.
2. `sales.selfserve`: owns the self-serve quote and signup flow.
3. `sales.service`: owns sales service operations.
4. `referrals.program`: owns Party-first capture policy, canonical ReferralCode,
   Referral and exact-Party account-attachment records, qualification/reward
   policy, and atomic program transition orchestration.
5. `referrals.account_conversion`: owns exact Referral/Party/Lead context
   validation, the bounded public-signup capability contract, and atomic
   account-creation/adjudication orchestration.

Rule: sales order, self-serve quote/signup, sales service, and Refer & Earn
referral logic resolve through these owners. `web_sales`/`web_referrals` adapters
and API/task callers request an outcome. `customer.accounts` creates or prepares
Subscriber rows; the referral coordinator never constructs them itself.
Customer referral reads and writes are native-only. The legacy referral mirror
is isolated compatibility evidence and never a SOT, decision, identity, or
attribution owner.
Quote-request and deposit surfaces branch on the explicit
`quotes_native_write_enabled` cutover control: the native branch is owned by
`sales.selfserve`, and its deposit "already paid" decision belongs to the paid
deposit Invoice in the billing ledger — never to a mirror flag the CRM could
stale-sync.
