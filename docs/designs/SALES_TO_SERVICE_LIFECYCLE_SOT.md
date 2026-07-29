# Sales-to-Service Lifecycle Source of Truth

**Status:** Approved and implemented by migration 389
**System of record:** Sub
**Decision owner:** Michael

## Contract

```text
signed interaction / staff capture
  -> IntegrationInbox receipt (when external)
  -> Party + immutable Lead origin
  -> exact Lead/Party account conversion
  -> Quote
  -> SalesOrder
  -> Project + InstallationProject
  -> WorkOrder(s), optionally scoped to ProjectTask
  -> staff-verified implementation evidence
  -> ServiceOrder release
  -> successful provisioning result
  -> active Subscription
  -> ready CX handoff
  -> staff CX acceptance
  -> fulfilled SalesOrder
  -> ongoing support / service history
```

The Sales-to-Service chain uses structural foreign keys. The still-authoritative
legacy invoice/payment side does not yet satisfy that rule: installation
invoice provenance and CRM payment idempotency still use metadata/external
identifiers until ADR 0007's financial phases cut over. The Phase 1 shadow
chain below uses `SalesOrderLine -> ServiceOrder -> Subscription` plus structural
contract/obligation identities; it does not make the legacy joins canonical.

## Owner-output chain

Each owner's committed transition stages its versioned output event in the
same transaction; the event dispatcher delivers it after commit with durable
retry, and the registered `SalesLifecycleProjectionHandler` adapter asks the
next owner to apply the consequence. A failed consequence stays a failed
event delivery — visible and retryable — never a warning log.

Funding, verified implementation, service-order release, and CX acceptance are
consumed through `sales.fulfillment`'s receipted owner commands
(`consume_funding_satisfaction` / `consume_verified_implementation` /
`consume_service_order_release` / `consume_cx_acceptance`). The funding
consumer's catalog, invoice, add-on, route, service-order, and payment helpers
are flush-only participants; its complete effect and unique
`(consumer, event_id)` receipt commit atomically via `events.owner_outputs`
(ADR 0007 §2), so redelivery is an exact no-op.

After the legacy funded consequence is staged, `sales.fulfillment` emits a
structural Phase 1 shadow input. `billing.contracts` receipts it, records
proposed terms, and emits obligation inputs; `billing.obligations` receipts
those, records the first proposed period, and emits the terminal result;
`billing.shadow_verification` receipts the terminal output and records
content-addressed evidence. Every row remains `shadow` and no target record
drives an invoice, payment, balance, access decision, or funding transition.

```text
sales_order.funding_satisfied   (sales.orders, atomically with the paid edge)
  -> pending Subscription + draft ServiceOrder per service line
     + order payment evidence            [sales.fulfillment, receipted]
  -> sales.fulfillment.funding_applied
  -> proposed BillingContractVersion     [billing.contracts, shadow + receipted]
  -> proposed first-period obligation    [billing.obligations, shadow + receipted]
  -> terminal delivery evidence          [billing.shadow_verification, receipted]
vendor_project.verified         (operations.vendor_project_lifecycle)
  -> project completion + ServiceOrder release  [sales.fulfillment]
service_order.released          (operations.service_order_lifecycle)
  -> sales-linked order enters provisioning     [service_order_lifecycle]
service_order.assigned
  -> provisioning run starts                    [ProvisioningHandler]
provisioning.completed/failed
  -> readiness decision -> activation           [operations.provisioning_lifecycle]
service_order.completed
  -> ready CX handoff                           [customer.experience_handoff]
customer_experience.accepted
  -> fulfilled SalesOrder                       [sales.orders]
```

The self-serve deposit path stages the same funding output with
`record_order_payment=false`, because the deposit's only ledger event is the
verified deposit-invoice payment.

## Named owners

| Decision or fact | Owner |
| --- | --- |
| Verified provider receipt | `integration.inbox` |
| Party-first capture and source replay | `sales.capture` |
| Immutable origin | `sales.lead_lifecycle` |
| Exact Lead/Party to account conversion | `sales.account_conversion` |
| Pipeline and Quote | `sales.service` |
| Sales Order and financial status | `sales.orders` |
| Structural shadow contract terms | `billing.contracts` |
| Structural shadow obligation identity | `billing.obligations` |
| Shadow delivery and cutover-run evidence | `billing.shadow_verification` |
| Project and implementation-scope coordination | `sales.fulfillment` calling `operations.project_lifecycle` |
| Vendor execution and verification evidence | `operations.vendor_project_lifecycle` |
| Committed cross-owner consequence delivery | registered `SalesLifecycleProjectionHandler` adapter |
| Work-order command and ProjectTask binding | `operations.work_order_commands` |
| ServiceOrder transitions and provisioning consequence | `operations.service_order_lifecycle` |
| Subscription/access transition | `access.subscription_lifecycle` |
| CX readiness, attention, and acceptance evidence | `customer.experience_handoff` |
| Aggregate drift report | `customer.lifecycle_audit` |
| Idempotent projection repair | `sales.lifecycle_reconciliation` |

Routes, templates, webhooks, event handlers, jobs, and commands are adapters.
They authorize/verify input, call the owner, and translate transport-neutral
errors. They do not write lifecycle state directly. Domain services must not
depend on HTTP request/response or exception types.

## Selfcare CRM Leads page contract

- Screen identifiers: `sales-leads-list`, `sales-lead-create`,
  `sales-lead-edit`, and `sales-lead-detail`.
- Audience and job: authorized sales staff triage opportunities, maintain the
  commercial context of an existing Party/Subscriber identity, and start a
  lead-backed Quote.
- Authoritative owners: `sales.lead_lifecycle` owns Lead identity/origin and
  Party alignment; `sales.service` owns Lead, Pipeline, Stage, summary, and
  Quote projections and commands; Party/Subscriber services own contact
  identity; RBAC owns `crm:lead:{read,write,delete}` and quote permissions.
- First viewport: Lead identity, status, value, owner, pipeline/stage,
  authoritative KPI summary, common filters, and the next permitted action.
- Actions: create/edit/status use `crm:lead:write`; list/detail use
  `crm:lead:read`; delete uses `crm:lead:delete`; quote creation uses
  `crm:quote:write` and navigates to the quote editor with the Lead selected.
- List contract: server-side search across Lead title plus authoritative
  contact name/email/phone, status/pipeline/stage/owner/source filters,
  updated/created ordering, 10/25/50/100 page sizes, and URL-preserved state.
- Summary contract: Total, Open, Won, and Pipeline Value come from
  `sales.service`; Open is New/Contacted/Qualified/Proposal/Negotiation and
  won/lost value is excluded.
- States: permission denial is enforced by route dependencies; forms preserve
  validated input and field errors; empty, loading/submitting, API failure with
  retry, duplicate-open-lead, and unavailable-contact states are explicit.
- Responsive projection: mobile retains Lead, contact, status, value, and the
  next action; lower-priority source/pipeline/owner columns progressively
  disclose at larger viewports. Dark mode uses the shared admin design tokens.
- Out of scope: no activity timeline, appointments, tasks, conversation
  history, import/export, bulk Lead commands, aging analytics, or parallel
  Lead persistence is introduced by these screens.

## Configuration versus code contracts

Operational values are not embedded in orchestration code. The default sales
implementation type, project status, project priority, provisioning workflow,
and connector header names come from domain settings or version-pinned
connector configuration. Provider-specific payload mapping belongs to the
installed connector/edge adapter.

Stable protocol vocabulary remains checked-in code: enum states, legal
state-machine edges, typed event names, capability IDs, idempotency-key formats,
and policy versions. Those are reviewed contracts, not mutable operating
configuration. Changing one requires a migration/versioned contract and tests.

## Lifecycle gates

1. Capture never creates a Subscriber implicitly and never deduplicates a
   person by email, phone, name, or social handle. Exact provider-event replay
   is idempotent; different content under the same event identity is rejected.
2. Account conversion locks the Lead and requires its exact Party. It creates
   or attaches one Subscriber through the account and Party owners, activates
   the customer role, and leaves the subscriber role pending.
3. Every non-cancelled SalesOrder receives at most one structurally linked
   Project and InstallationProject. ProjectTask may own several WorkOrders;
   WorkOrder owns the foreign key.
4. A partially paid SalesOrder records the receipt but creates no Subscription
   or ServiceOrder. Full funding stages `sales_order.funding_satisfied`
   atomically with the paid transition; the lifecycle projection handler
   creates one pending Subscription and one idempotent ServiceOrder per
   service line through `sales.fulfillment.consume_funding_satisfaction`. The
   same receipted transaction stages the Phase 1 structural shadow input. An
   unresolved consequence (for example an offer that no longer resolves)
   fails the delivery visibly instead of being skipped.
5. Sales ServiceOrders remain `draft` until the vendor-project owner records an
   append-only staff verification event. After that fact commits, the registered
   lifecycle projection handler asks `sales.fulfillment` to complete the native
   Project and release linked ServiceOrders. Replay is idempotent and failure is
   retryable; the vendor owner never writes project or provisioning roots.
   The committed `service_order.released` output then moves the sales-linked
   ServiceOrder into `provisioning` through its lifecycle owner; repair and
   reprovisioning orders keep manual progression.
6. Billing cannot directly activate a sales-created pending Subscription.
   Only a successful provisioning result may transition the linked ServiceOrder
   to `active`; that transition asks the subscription owner to activate access.
7. Successful activation emits the committed service-order completion fact.
   The lifecycle projection handler asks the CX owner to create a handoff only
   when funding, implementation, provisioning, and Subscription evidence all
   agree. CX staff acceptance is separately actor/time/reason evidenced; its
   committed `customer_experience.accepted` output asks `sales.orders` to
   fulfil the SalesOrder. The CX owner does not write sales state inline.
8. Support Tickets and ticket-origin WorkOrders stay attached to the same
   Subscriber/Party history but do not rewrite sales attribution.

## Failure and repair

Money, implementation verification, provisioning outcomes, and CX acceptance
are never inferred. A downstream failure retains already-authoritative facts
and is retried by projection/reconciliation.

Run the PII-free audit:

```bash
python -m scripts.migration.audit_customer_lifecycle
```

Preview repairable projection drift:

```bash
python -m scripts.migration.reconcile_sales_lifecycle
```

Apply only owner-backed repairs:

```bash
python -m scripts.migration.reconcile_sales_lifecycle --apply
```

The reconciler may create a missing implementation scope, release a ServiceOrder
from existing verification evidence, or recreate a missing ready CX handoff. It
cannot invent an interaction, Party binding, payment, verification event,
provisioning result, or acceptance.
