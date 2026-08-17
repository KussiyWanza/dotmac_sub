# Sales-to-Service Lifecycle Source of Truth

**Status:** Approved and implemented through migration 480
**System of record:** Sub
**Decision owner:** Michael

## Contract

```text
signed interaction / staff capture
  -> IntegrationInbox receipt (when external)
  -> Party + immutable Lead origin
  -> manually authored Lead-backed Quote(s)
  -> accepted Quote
  -> exact Lead/Party account conversion + Lead Won
  -> SalesOrder + copied Quote lines
  -> Project + InstallationProject + configured ProjectTemplate Tasks
  -> configured WorkOrder(s), each scoped to its ProjectTask
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

## Approved reusable sales extraction boundary (2026-08-17)

The reusable sales owner stops at an **accepted Quote**. A Starter-owned
`dotmac-sales` module will own Leads, Pipelines, opportunity Stages, Quote
authoring, Quote lifecycle, acceptance, and the immutable accepted commercial
snapshot. Its committed consequence is a versioned, product-neutral
accepted-quote owner output delivered after commit with durable retry and an
exactly-once consumer receipt.

The module will not own or construct a Subscriber, SalesOrder, Project,
ProjectTask, InstallationProject, WorkOrder, invoice, ServiceOrder, or
Subscription, and it will not import `dotmac-orders`. Those are downstream
product/domain consequences of the accepted-quote output. Provider transports,
campaigns, Inbox/conversations, WhatsApp, consent and retention case management
are also outside the module.

This amendment records the target authority boundary; it does not rewrite the
as-built chain below. Until the module passes the Starter lineage gate, is
adopted by Sub, backfilled, shadow-compared and reconciled, the existing Sub
services and tables remain authoritative and `sales.quote_acceptance` retains
its current atomic cross-domain transaction. The migration must deliberately
split that transaction at the accepted-quote commit/output boundary; it may not
run a second writer beside the current one.

The reconciled CRM owner map and its deliberately unresolved campaign and
retention rows are in
[`MARKETING_SALES_SOT.md`](MARKETING_SALES_SOT.md).

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
verified deposit-invoice payment. The initial accepted Quote records normalized
deposit reference, amount, and provider evidence. An exact verification retry
replays the same conversion and SalesOrder bookkeeping; changed evidence fails
closed before SalesOrder money can be overwritten.

## Named owners

| Decision or fact | Owner |
| --- | --- |
| Verified provider receipt | `integration.inbox` |
| Party-first capture and source replay | `sales.capture` |
| Atomic admin Person and Lead authoring and maintenance | `sales.lead_authoring` |
| Immutable origin | `sales.lead_lifecycle` |
| Atomic Lead-backed New Quote authoring | `sales.quote_authoring` |
| Atomic Quote acceptance and sales conversion | `sales.quote_acceptance` |
| Flush-only exact Lead/Party account conversion participant | `sales.account_conversion` |
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
  commercial context of an existing Party identity, and manually start one or
  more Lead-backed Quotes without creating a customer account.
- Authoritative owners: `sales.lead_lifecycle` owns Lead identity/origin and
  Party alignment; `sales.service` owns Lead, Pipeline, Stage, summary, and
  Quote projections and commands; Party/Subscriber services own contact
  identity; RBAC owns `crm:lead:{read,write,delete}` and quote permissions.
- First viewport: Lead identity, status, value, owner, pipeline/stage,
  authoritative KPI summary, common filters, and the next permitted action.
- Actions: create/edit/status use `crm:lead:write`; list/detail use
  `crm:lead:read`; delete uses `crm:lead:delete`; quote creation uses
  `crm:quote:write` and navigates to the quote editor with the Lead selected.
- Edit contract: the editor loads the existing Person Party and Lead into the
  same complete profile/opportunity form used for creation. One typed
  `sales.lead_authoring` maintenance command preserves the exact Party binding,
  canonicalizes a legacy Subscriber-only Lead to that Subscriber's already
  reviewed Person Party without contact-value identity inference,
  reconciles active email/phone/WhatsApp contact points without deleting their
  verification or consent history, updates the Person profile and optional
  Organization relationship, and stages editable Lead values, audit, and a
  PII-free `lead.updated` event in one transaction. Blank NIN input preserves
  the stored encrypted value; immutable origin/source and converted-account
  reseller ownership fail closed.
- List contract: server-side search across Lead title plus authoritative
  contact name/email/phone, status/pipeline/stage/owner/source filters,
  updated/created ordering, 10/25/50/100 page sizes, and URL-preserved state.
- Query contract: `sales.service` accepts one typed Lead list input, collapses
  search whitespace, canonicalizes stale filters/sort/page values, and applies
  one shared predicate set to rows, unique count, pagination, and summary.
  Party, active Party email/phone contact points, and Subscriber matches use
  correlated `EXISTS`; full Lead rows (including PostgreSQL `json` metadata)
  are never subjected to `DISTINCT`.
- Summary contract: Total, Open, Won, and Pipeline Value come from
  `sales.service` over the active search/filter scope; Open is
  New/Contacted/Qualified/Proposal/Negotiation and won/lost value is excluded.
- States: permission denial is enforced by route dependencies; forms preserve
  validated input and field errors; empty, loading/submitting, API failure with
  retry, duplicate-open-lead, and unavailable-contact states are explicit.
- Responsive projection: mobile retains Lead, contact, status, value, and the
  next action; lower-priority source/pipeline/owner columns progressively
  disclose at larger viewports. Dark mode uses the shared admin design tokens.
- Out of scope: no activity timeline, appointments, tasks, conversation
  history, import/export, bulk Lead commands, aging analytics, or parallel
  Lead persistence is introduced by these screens.

## Selfcare CRM Quotes list page contract

- Screen identifier and route: `sales-quotes-list` at
  `/admin/sales/quotes`.
- Audience and job: authorized sales staff find and compare active commercial
  proposals by Quote identity, status, Lead, Party, contact, and optional
  Subscriber context.
- Authoritative owner: `sales.service` accepts one typed Quote list input and
  returns one typed outcome containing the normalized scope, exact unique
  count, and deterministically ordered page. `app.services.web_sales`, the
  route, and the template translate and render that contract; none owns a
  parallel search predicate.
- Search contract: whitespace is collapsed and an empty result means no
  search. Quote UUID (including partial UUID), Lead title, Party display name,
  every active Party contact-point display/normalized value, and optional
  Subscriber display/first/last/email values are matched case-insensitively.
  Inactive Party contact points never match. LIKE metacharacters are escaped
  and bound as parameters.
- Query shape: the outer result and count select directly from active Quotes.
  Related-record matches use correlated `EXISTS`; one-to-many contact points
  never multiply Quote rows, and PostgreSQL never applies full-row `DISTINCT`
  to the Quote's `json` metadata column. The exact same predicate tuple drives
  count and rows before stable created/updated ordering, Quote-ID tie-breaking,
  and pagination.
- Filters and state: status and Lead filters work independently and combine
  with search using AND semantics. Unknown status, malformed/stale Lead,
  sort, direction, page, and page-size values canonicalize to the owner-defined
  safe URL. Search/filter/sort/page-size state remains URL-addressable; changing
  the form resets page to one and Reset clears the complete scope.
- States and recovery: empty and database-failure states are distinct. A failed
  read reports that Quotes could not be loaded and no CRM data was changed,
  offers a retry using safe normalized list state, emits a structured diagnostic
  without the search term, and performs no writes.
- Responsive projection: filters stack on narrow screens and the table retains
  Quote identity, status, value, related Lead/customer context, and its direct
  detail link.

## Selfcare New Quote page contract

- Screen identifier and route: `sales-quote-create` at
  `/admin/sales/quotes/new`, posting to `/admin/sales/quotes` with
  POST-Redirect-GET and HTTP 303 on success.
- Audience and job: staff with `crm:quote:write` create a pricing proposal for
  exactly one eligible Lead or eligible active Customer while retaining the existing optional Install Location.
- Decision owners: `sales.quote_authoring` owns typed validation, Lead/Party
  recipient resolution, line-reference validation, Decimal calculations,
  metadata enrichment, Draft/Sent initial status, idempotency, audit, and
  transactional event staging. `sales.quote_acceptance` exclusively owns the
  later Accepted transition and conversion. Tax configuration, Lead lifecycle, account,
  order, Project, Task, WorkOrder, and fulfillment owners retain their named
  decisions.
- Identity contract: staff select exactly one Lead or Customer. A customer search
  is server-backed and exposes only active accounts with reviewed active Party
  bindings. `sales.customer_quote_linkage` locks the submitted Customer and
  reuses (or creates) its unique system Lead; the Quote remains Lead-backed and
  also carries the existing Subscriber id. Browser values never establish Party,
  account, or owner identity; the authenticated SystemUser supplies ownership.
- First viewport: Quotes breadcrumb, New Quote title and purpose, mutually
  exclusive Lead and Customer pickers (one required),
  Draft-default status, NGN-default currency, required Project Type, and the
  start of the responsive Line Items editor.
- Authoring contract: one empty row remains visible; completely empty rows are
  ignored; custom descriptions are allowed; active Selfcare offers and native
  field-inventory items are suggested in batches; submitted identifiers are
  batch-resolved and must match their descriptions. Amount, Subtotal, configured
  Tax Total, and Total are server-derived with Decimal money rounding. New Line
  Items are gross-priced. One optional mutually exclusive percentage or fixed
  Quote discount applies to the complete subtotal before configured tax; it
  records the authenticated SystemUser and server time, and its reason is
  optional. Manual Tax Total is accepted only without a configured Tax Rate.
- Lifecycle contract: new Quotes may be Draft or Sent only. Draft has no
  downstream consequences and Sent sets `sent_at`; Accepted is a separate
  action invoking the atomic acceptance coordinator. Rejecting or expiring one
  of several Quotes does not close the Lead. Exact submission replay returns
  the same Quote, while conflicting reuse fails closed.
- States and recovery: ordinary validation failures render an accessible error
  banner and preserve all scalar, location, line, and suggestion-identifier
  values. An active Tax Rate with an invalid percentage is excluded from the
  selectable projection, emits structured drift evidence, and renders a
  partial-data warning instead of preventing Quote authoring. Native browser
  constraints cover required Lead, currency, and numeric bounds. The submit
  control exposes a Submitting state and rejects an in-flight duplicate
  submission.
- Responsive projection: the form card is centered at `max-w-3xl`; multi-column
  rows stack on narrow screens; each Line Item becomes a touch-friendly card;
  keyboard focus, accessible labels, and light/dark variants use shared admin
  design tokens.

## Quote discounts history page contract

The complete command, evidence, migration, and page contract is
`docs/designs/QUOTE_DISCOUNT_HISTORY.md`. `sales.quote_authoring` owns current
discount application, replacement, removal, recalculation, and append-only
history. `sales.quote_discount_reporting` owns the filtered staff projection at
the Quote tab of `/admin/reports/discounts`; `ui.document_discount_report` owns
the typed administrative page projection. The previous route redirects to the
report. Previous Quote Line Item discounts remain read-only historical evidence
and new writers always store zero in that legacy field.

## Configuration versus code contracts

Operational values are not embedded in orchestration code. Staff select the
Quote's Project Type; the active `ProjectTemplate.project_type` mapping assigns
the template without a hard-coded template identifier. Project status, project
priority, provisioning workflow, and connector header names come from domain
settings or version-pinned connector configuration. Provider-specific payload
mapping belongs to the installed connector/edge adapter.

Stable protocol vocabulary remains checked-in code: enum states, legal
state-machine edges, typed event names, capability IDs, idempotency-key formats,
and policy versions. Those are reviewed contracts, not mutable operating
configuration. Changing one requires a migration/versioned contract and tests.

## Lifecycle gates

1. Capture never creates a Subscriber implicitly and never deduplicates a
   person by email, phone, name, or social handle. Exact provider-event replay
   is idempotent; different content under the same event identity is rejected.
2. A Quote is authored manually from an exact Lead or Customer and requires a selected
   Project Type. The typed `Quote.project_type` column is the authoritative
   downstream input; the metadata key is a compatibility projection only. The Lead
   may have multiple Quotes, and Draft/Sent Quote authoring creates no
   Subscriber, SalesOrder, Project, ProjectTask, InstallationProject, or
   WorkOrder. Accepted is a separate transition.
3. `sales.quote_acceptance` is the only sales conversion event. It locks the
   Quote and Lead and, in one owner transaction, marks the Lead Won, creates or
   attaches the exact Subscriber, copies the Quote and lines into one
   SalesOrder, copies the Quote Project Type to one Project, assigns the active
   ProjectTemplate configured for that type, creates its Tasks and one
   InstallationProject, creates WorkOrders only for template tasks whose
   automation policy is enabled, and stages audit and outbox events. A missing
   template or any participant/event failure rolls the complete change back.
   A Draft or Sent Quote whose expiry is at or before the locked acceptance
   decision time fails closed with no downstream records. Durable event
   delivery occurs only after commit. The accepted Quote and its line items are
   then an immutable commercial snapshot matching the copied SalesOrder;
   revised terms require a new Quote rather than editing or deactivating the
   accepted evidence. `sales.orders` allocates the copied SalesOrder number
   under the locked `sales_order_number` document sequence. Existing canonical
   `SO-<digits>` SalesOrders are issued-number evidence: if import, restore, or
   operator drift leaves the cursor behind the highest issued number, the
   allocator advances it before reservation so acceptance repairs the drift
   without weakening the unique-number constraint.
   For an admin-authored Lead, optional reviewed external reseller ownership is
   copied into `SubscriberCreate`; absence resolves through the customer-account
   owner to the House reseller. Downstream records remain structurally scoped by
   Subscriber and must not copy or infer reseller ownership from contact email.
4. Acceptance replay is idempotent by Quote identity. Structural unique keys
   and deterministic ProjectTask WorkOrder keys return the canonical account,
   SalesOrder, Project, Tasks, and WorkOrders without duplicates. Each created
   ProjectTask captures its template WorkOrder automation decision. Replay
   creates only a missing WorkOrder required by that captured decision,
   preserves existing and manual WorkOrders, and does not apply later template
   edits retroactively. Generic ProjectTask metadata edits preserve these
   owner-captured policy keys. Legacy ProjectTasks without a captured decision
   use the currently linked active template task as a repair fallback. A conflicting
   Lead, Party, account, or lifecycle state fails closed. Every Quote or line
   mutation locks the parent Quote first, so an edit cannot race acceptance and
   produce stale copied money. An already accepted replay does not re-evaluate
   expiry; it returns or repairs the same canonical conversion records. When
   deposit evidence is supplied, replay additionally requires the same
   normalized reference, amount, and provider. Changed evidence is a conflict
   and cannot rewrite SalesOrder payment fields.
5. Every non-cancelled SalesOrder receives at most one structurally linked
   Project and InstallationProject. Users may create a WorkOrder against the
   Project or an individual ProjectTask. ProjectTask may own several
   WorkOrders; WorkOrder owns the foreign key.
6. A partially paid SalesOrder records the receipt but creates no Subscription
   or ServiceOrder. Full funding stages `sales_order.funding_satisfied`
   atomically with the paid transition; the lifecycle projection handler
   creates one pending Subscription and one idempotent ServiceOrder per
   service line through `sales.fulfillment.consume_funding_satisfaction`. The
   same receipted transaction stages the Phase 1 structural shadow input. An
   unresolved consequence (for example an offer that no longer resolves)
   fails the delivery visibly instead of being skipped.
7. Sales ServiceOrders remain `draft` until the vendor-project owner records an
   append-only staff verification event. After that fact commits, the registered
   lifecycle projection handler asks `sales.fulfillment` to complete the native
   Project and release linked ServiceOrders. Replay is idempotent and failure is
   retryable; the vendor owner never writes project or provisioning roots.
   The committed `service_order.released` output then moves the sales-linked
   ServiceOrder into `provisioning` through its lifecycle owner; repair and
   reprovisioning orders keep manual progression.
8. Billing cannot directly activate a sales-created pending Subscription.
   Only a successful provisioning result may transition the linked ServiceOrder
   to `active`; that transition asks the subscription owner to activate access.
9. Successful activation emits the committed service-order completion fact.
   The lifecycle projection handler asks the CX owner to create a handoff only
   when funding, implementation, provisioning, and Subscription evidence all
   agree. CX staff acceptance is separately actor/time/reason evidenced; its
   committed `customer_experience.accepted` output asks `sales.orders` to
   fulfil the SalesOrder. The CX owner does not write sales state inline.
10. Support Tickets and ticket-origin WorkOrders stay attached to the same
   Subscriber/Party history but do not rewrite sales attribution.

## Failure and repair

Quote acceptance is a synchronous structural boundary: no acceptance fact is
authoritative unless its account, order, implementation scope, configured
tasks/WorkOrders, audit, and outbox records all committed together. Money,
implementation verification, provisioning outcomes, and CX acceptance after
that boundary are never inferred. Accepted-Quote replay is also the repair
entrypoint for a missing captured-policy WorkOrder and leaves unrelated manual
work intact. A later downstream delivery failure retains already-authoritative
facts and is retried by projection/reconciliation.

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
