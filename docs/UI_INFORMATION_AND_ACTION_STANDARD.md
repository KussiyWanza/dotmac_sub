# UI Information and Action Standard

Status: approved cross-Dotmac standard (Michael, 2026-07-14).

This document governs which information a Dotmac screen presents, how deeply it
exposes that information, and which actions it offers. It complements the
backend source-of-truth architecture in `docs/SOT_RELATIONSHIP_MAP.md`.

The backend owner decides truth, state meaning, eligibility, and transitions.
The UI contract decides relevance, ordering, presentation depth, and
interaction. A route, template, HTMX fragment, mobile client, or JavaScript
component must not create a parallel business decision.

## Documentation Authority

Apply UI guidance in this order:

1. `UI_INFORMATION_AND_ACTION_STANDARD.md`: information, action, provenance,
   and progressive-disclosure policy.
2. `PRODUCTION_UI_BRIEF.md`: visual, density, page-anatomy, and interaction
   policy for the ISP operations console.
3. `FRONTEND_SPEC.md`: current implementation contracts, context shapes,
   macros, and template conventions.
4. `DESIGN_REVIEW_CHECKLIST.md`: the merge gate for UI-facing changes.

`DESIGN.md` is a token and implementation inventory. Historical plans,
comparisons, UX audits, screenshots, and feature proposals are evidence, not
current requirements. When they conflict with the four documents above, the
ordered authority above wins.

## Ownership Boundary

Each screen composes existing owners; it does not become a new business source
of truth.

- Domain read and context services own displayed facts and status meaning.
- Domain command and transition services own action eligibility and execution.
- RBAC services own authorization.
- Event and timeline services own official history.
- UI page contracts own projection, ordering, progressive disclosure, and
  responsive depth.
- Web/API routes, templates, HTMX handlers, and mobile clients render contracts
  and submit commands through the owners.

The same owner supplies equivalent state and action hints to web, API, and
mobile surfaces. Clients may adapt layout to their viewport and audience, but
must not recalculate business meaning.

## Information Depth

Every item belongs to one of four depths:

1. **Glance**: identity, current state, impact, urgency, owner, freshness, and
   the next valid action.
2. **Work**: filters, comparisons, queues, tables, and common operational
   actions.
3. **Investigation**: relationships, contributing facts, diagnostics, and
   history needed to understand a case.
4. **Evidence**: raw identifiers, immutable events, delivery attempts,
   reconciliation evidence, provider responses, and audit records.

Depth is selected by the user's job, permission, and current task. Customer
surfaces normally emphasize glance and work. Support, finance, field, and NOC
surfaces normally expose work and investigation. Evidence is available only to
roles and workflows that need it.

Progressive disclosure changes visibility, not truth. Every depth consumes the
same authoritative owner and status semantics.

## Required Page Contract

New dashboards, lists, detail pages, editors, control-plane pages, and material
redesigns define a page contract before implementation. Record it in the
feature design, the relevant service contract, or a declarative page-contract
registry when one exists.

The contract names:

- screen identifier and page type;
- audience and operational job;
- decision the screen supports;
- primary entity and human/service identifiers;
- authoritative read or context owner;
- first-viewport information;
- primary, secondary, row, bulk, and destructive actions;
- command owner and action-eligibility owner;
- visible fields and sensitivity classification;
- table columns, filters, default sort, pagination, totals, and export rules;
- status, reason, action-hint, provenance, and freshness fields;
- loading, empty, partial, stale, error, and unauthorized states;
- drill-down destinations;
- desktop and mobile projections;
- audit and observability requirements.

If the data or action owner cannot be named, the screen contract is incomplete.

## Relevance Test

Display an item only when it supports at least one of these purposes:

- identify the subject;
- understand current state;
- compare or prioritize work;
- assess impact, risk, value, or urgency;
- choose or perform a valid action;
- explain why a decision was made;
- provide evidence required by the role.

Do not expose a model field merely because it exists. Do not show vanity KPIs,
permanently zero charts, duplicate statuses, unexplained internal flags, or
technical metadata ahead of the operational decision.

Exceptions and actionable risks precede aggregate totals. Derived values show
their relevant period, scope, currency/unit, provenance, and freshness.

## Page-Type Contracts

### Dashboard

A dashboard answers "what needs attention now?" before "what happened over
time?"

- Lead with current health and actionable exceptions.
- Use only decision-bearing KPIs; dashboard strips contain 4-6 items.
- Each KPI links to the exact filtered cohort that produced it.
- Show one primary work queue before lower-priority charts or live widgets.
- Use no more than two equally weighted charts above the first work surface.
- Do not use dashboards as module directories or marketing pages.

### List Or Queue

A list supports scanning, comparison, prioritization, and repeated action.

- Lead with search and common filters directly above the table.
- Show identity, state, impact/value, owner, relevant time, and next action.
- Default ordering reflects operational urgency or the most relevant recency.
- Preserve active filters, sort, tenant scope, and pagination in drill-down and
  export links.
- Show bulk actions only after selection and only when one canonical command
  service supports the cohort safely.

### Detail

A detail page establishes the decision context before exposing exhaustive data.

- The first viewport shows identity, authoritative state, reason, impact,
  ownership, freshness, and the next valid action.
- Group later sections by operator task, not by ORM model.
- Keep related records, diagnostics, timeline, and audit evidence at increasing
  depth.
- Keep customer, subscription, financial, access, network, device, outage, and
  support states distinct when more than one matters.
- For work orders, linked ticket, project, and project-task surfaces route to
  the canonical work-order detail view. Creation and technician assignment are
  displayed as consecutive actions because their command owners remain
  separate; creation must not imply that a technician was assigned.
- Material requests remain work-order-owned. Ticket, project, and project-task
  detail pages project requests from their linked native work orders and scope
  the create action to an actively assigned work order; they do not maintain
  duplicate material-request relationships.

### Editor Or Form

An editor describes a transition, not just a set of database fields.

- Show current state and the proposed state.
- Explain prerequisites and validation failures near the affected control.
- Preview financial, service, customer, or network impact before a high-impact
  change.
- Name irreversible consequences and required evidence.
- On success, expose the resulting state, event, or asynchronous operation.

### Control Plane

A control-plane page shows effective behavior, not only editable storage.

- Show effective value, source, override precedence, affected scope, and last
  change.
- Distinguish policy gates from tuning values and temporary migration flags.
- Link to audit history and relevant task/health evidence.
- Do not offer a control whose consumer or owner cannot be identified.

### Incident Or NOC

- Lead with severity, blast radius, affected customers/services, owner,
  freshness, and escalation state.
- Group related observations into operational incidents before exposing raw
  alert streams.
- Put probable cause and topology context before low-level telemetry.
- Keep acknowledgement, investigation, resolution, and communication actions
  distinct.

## Table Standard

Tables are the primary work surface for repeated operational workflows.

- Every visible column supports identity, comparison, decision, action, or
  evidence.
- Default to 5-8 business columns plus one action column. Document exceptions
  for comparison-heavy or audit tables.
- Put secondary fields on the detail page or in an explicit column chooser.
- Keep common filters visible; place advanced filters behind progressive
  disclosure.
- Search by domain identifiers used in real operations.
- Use server-side filtering, sorting, pagination, totals, and exports for large
  datasets.
- Apply RBAC and tenant scope to the query and export, not only the rendered
  controls.
- Keep numeric and financial values aligned and units/currency explicit.
- Make technical identifiers copyable and visually distinct.
- Never communicate status by color alone.
- Preserve table dimensions across loading, empty, partial, and error states.
- Mobile projections retain identity, state, impact, and next action instead of
  squeezing every desktop column into the viewport.

Unknown, stale, and unavailable values sort and filter according to explicit
domain rules; they must not silently become zero.

## Action Standard

- Provide exactly one page-level primary action.
- Provide at most one common visible row action; put additional actions in a
  consistent overflow menu.
- Hide unauthorized actions. Show a disabled action only when explaining its
  state-based unavailability helps the user.
- Read eligibility, required amount, restoration possibility, completion
  readiness, and destructive impact from the owning backend service.
- Never rely on hidden UI controls as enforcement; the command owner rechecks
  authorization and eligibility at execution time.
- Require an impact preview and explicit confirmation for destructive,
  financial, customer-visible, fleet-wide, filtered-bulk, or all-customer
  changes.
- Return an operation or event identifier for asynchronous actions and show
  progress without claiming an optimistic final state.
- Audit administrative mutations through the canonical audit/event owner.

For prepaid recovery, the service page consumes the recovery eligibility
owner's typed next action. An unresolved service invoice disables Bill Now,
explains the block, and links to that exact invoice. The invoice page keeps
settlement-backed payment credit, reviewed opening funding, and exact shortfall
separate; generic displayed account balance is not settlement evidence.

Familiar icon-only controls are appropriate for compact tools when they have an
accessible label and tooltip. Business commands use clear text or icon-and-text
labels.

## State, Provenance, And Freshness

The following states are never interchangeable:

- unknown, zero, and not applicable;
- stale, unavailable, and failed;
- disabled and unauthorized;
- subscription lifecycle and service access;
- invoice/receivable state and payment state;
- device reachability and customer impact;
- individual service failure and grouped outage.

When operationally relevant, render status with its reason, source, observed
time, and stale threshold. A cache or mirror may render last-known state only
when the UI identifies it as such.

## Cross-Domain Identity

Customer and service workflows preserve identity across modules. Depending on
the task, the contract may include account number, subscriber code, phone,
PPPoE username, ONT serial, MAC, IP, OLT/PON location, invoice number, ticket,
or work-order identifier.

The owning customer-context service composes these relationships. Templates and
clients do not rediscover joins or infer ownership from imported identifiers.

## Responsive Projection

Responsive design changes arrangement and depth, not semantics.

- Preserve identity, state, impact, and next action before secondary details.
- Move action rails below summaries rather than overlaying content.
- Stack filters before reducing legibility.
- Replace wide evidence tables with an ordered summary plus drill-down.
- Keep controls and fixed-format elements dimensionally stable.
- Do not hide the only explanation for a status or action on mobile.

## Enforcement

UI-facing changes must:

1. Complete the information/action section in the pull request template.
2. Apply `docs/DESIGN_REVIEW_CHECKLIST.md`, marking irrelevant items `N/A`.
3. Add or update a page contract for material new screens or redesigns.
4. Test action eligibility at the owning service boundary.
5. Test KPI-to-filtered-cohort parity when a KPI drills into a list.
6. Test unknown, stale, unavailable, empty, partial, error, and unauthorized
   states relevant to the screen.
7. Verify standard desktop and mobile viewports for first-viewport usefulness,
   overflow, stable dimensions, and action hierarchy.

Architecture tests should prevent routes and templates from querying ORM state
or deriving domain status and action eligibility when an owner service exists.
Shared components should encode these defaults without becoming business-policy
owners.

## Migration Of Existing Screens

Existing screens migrate incrementally:

1. Record the current page contract and identify unsupported information or
   actions.
2. Name the authoritative read, status, eligibility, command, and timeline
   owners.
3. Move the first viewport and common actions onto those contracts.
4. Reconcile KPI, table, detail, export, web, API, and mobile projections.
5. Remove template/client inference and dead controls.
6. Add focused contract and browser tests.
7. Retire obsolete page-specific helpers and conflicting documentation.

Historical plans may provide requirements or research, but each item must be
revalidated against this standard and the current domain SOT before
implementation.

## Inbox Lead Intake And Catalogue Action Contract

- Audience and task: a prospect who is neither a customer nor a customer contact
  supplies identity and service location; Sales administrators manage immutable
  form versions; Inbox staff can issue, revoke, and reissue links. Any Inbox
  participant may receive a plan-family catalogue.
- Authority: `sales.lead_intake` owns template, eligibility, invitation, and
  completion decisions. `service_intent.plan_family_catalogues` owns catalogue
  publication, versioning, and current-document resolution. The public and admin
  routes and templates are adapters.
- First viewport: form purpose, customer-type fields, address search and
  confirmation, privacy notice, error state, and one primary Save action.
- Action eligibility: automatic send is disabled until explicitly enabled and
  both template types are published. Manual Lead actions require
  `crm:lead:write`, a reply-capable supported channel, and owner-resolved proof
  that the sender is neither a customer nor a customer contact; ambiguous
  identity fails closed. The Lead action is rendered beside the conversation
  composer. The catalogue action is visible for every conversation and enables
  only plan families with a currently published PDF and a reply-capable thread.
- State semantics: issued, effectively expired, revoked, completed, and failed
  delivery are distinct. Unknown/expired tokens render the same unavailable
  response and never reveal whether a digest exists.
- Responsive behavior: fields stack on small screens, controls retain a
  44-pixel target, and address results remain adjacent to their search field.

## Customer Quote Payment Page Contract

- Audience and task: an authenticated customer reviews and pays the exact
  deposit for a quotation owned by an authorized subscriber identity.
- Authority: `app.services.quote_deposits` resolves quotation eligibility and
  the server-owned deposit amount; `financial.payment_routing` resolves
  Paystack availability; established invoice, intent, payment-verification, and
  Quote-acceptance owners retain every financial transition. The route and
  template are adapters.
- First viewport: quotation identity, expiry, authoritative currency and
  deposit amount, secure-payment explanation, and one Paystack action.
- GET state: authentication, ownership, active status, expiry, paid state,
  positive deposit, and Paystack availability are checked without creating an
  invoice or payment intent. Missing or unauthorized quotations render the same
  not-found state.
- Mutation: the customer confirms through the CSRF-protected POST intent route.
  The request carries idempotency evidence only; it cannot submit amount,
  currency, invoice identity, or provider choice. The server fixes the provider
  to Paystack and re-derives the amount before delegating to the established
  quotation-deposit capability.
- States: unauthenticated, unauthorized/not found, expired, cancelled/inactive,
  already paid, Paystack unavailable, checkout failed, pending verification,
  and confirmed are distinct and fail closed.
- Responsive behavior: summary and action stack on small screens, retain the
  authoritative amount and primary action, and do not expose internal
  collection-account or payment-intent identifiers.

## Inbox Customer Context Page Contract

- Authority: `communications.team_inbox_contact_context` owns the typed drawer
  projection and `communications.inbox_lead_actions` owns action resolution.
- Zero means a successful authoritative collection query returned no rows.
  `—`, `Unavailable`, `Not calculated`, and `Restricted` remain separate states.
- Customer-specific examples and fabricated fallback values are prohibited.
- Profile and Lead actions are permission-scoped server outcomes. The browser
  never selects identity, pipeline defaults, or duplicate-prevention policy.
- Successful actions return to the exact originating conversation and trigger
  a fresh drawer query; read failure never replays the mutation.
- The drawer keeps customer identity visible while Details and Conversations
  tabs provide progressive disclosure. The Conversations badge is the
  authoritative full count of previous conversations for the exact Subscriber;
  its bounded newest-first list shows channel, status, and last activity and
  routes each row to the exact prior Inbox conversation. Assignment does not
  narrow this customer history.

## ONT Configure Page Contract

- Audience and task: authorized network staff submit one customer-service
  configuration section for an exact active ONT assignment and follow its
  asynchronous delivery through verified readback.
- Authority: `network.ont_service_configuration` owns lifecycle, revision,
  operation projection, failure/waiting reason, and next action. Assignment,
  WAN intent, PPP credential, delivery authorization, reconciler, operation,
  and dispatch owners retain their narrower contracts.
- Mutation: the POST adapter parses transport values into the typed command and
  maps its outcome. It performs no device call, reconcile call, task publish,
  business commit, status clearing, or action-eligibility decision.
- First viewport: exact assignment, configuration revision, operation ID,
  delivery phase, last verified observation, precise reason, and one
  owner-supplied next action.
- State semantics: saved, queued, applying, readback-pending, verified, failed,
  superseded, and retired are distinct. Saved or broker-delivered is never
  described as device configured.
- VLAN and credentials: show effective customer VLAN with provenance. PPPoE
  username is masked and identified as derived from the subscriber access
  credential; no PPPoE password input or display is permitted.
- Evidence: the current panel consumes only events bound to the active
  configuration head/revision. Unbound legacy and retired-assignment evidence
  is shown separately and cannot affect current action eligibility.
- Responsive behavior: desktop and mobile keep assignment, revision,
  operation, phase, reason, and action visible; HTMX refreshes the same owner
  projection rather than inferring progress in the browser.
