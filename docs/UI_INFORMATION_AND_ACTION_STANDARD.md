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
- The field Materials destination is requester-owned history. It remains
  available when the requester also has manager capabilities, shows the
  authoritative total before pagination, and exposes every owner-observed
  lifecycle state and rejection reason. List and detail refresh from the API;
  neither technician-profile lifecycle nor work-order reassignment may hide an
  exactly owned historical request.

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
- Navigation follows the same default-deny RBAC contract as its destination:
  do not render a link, shortcut, submenu, or section heading unless the
  principal holds a permission accepted by the destination route. Module
  enablement and authorization are independent gates and both must pass. Hide
  a navigation group when none of its children remain visible; route guards
  remain mandatory even when discovery is suppressed.
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
- When a gated action's blockers, evidence, and next steps are worth showing
  together (not just a single `disabled_reason` string), the owning service
  hands them to the caller as an `ActionReadiness` verdict
  (`app/services/action_readiness.py`) rather than inventing a new ad-hoc
  shape — see `docs/designs/ACTION_READINESS_CONTRACT.md`.
- A blocker's declared `owner` must be a real, decision-making backend
  service — never the readiness contract itself and never another
  pure-vocabulary UI layer.
- Render an `ActionReadiness` verdict through the shared
  `action_readiness_panel` macro
  (`templates/components/actions/action_readiness.html`); do not re-derive
  its state, tone, or audience-specific message in a template.
- `ActionForm.gated_by(readiness, …)` is the standard way to turn a readiness
  verdict into a form's `allowed`/`disabled_reason` — it changes no
  eligibility decision, only the transport shape reaching the form.

The admin subscription detail page presents an outstanding pending plan-change
request with its target plan, effective date, execution state, request identity,
and submission time. Operators with `catalog:write` may cancel that pending
request only after entering a reason and confirming that the subscription stays
on its current plan. The action is absent after approval or application and does
not offer a revoke path. While the request is outstanding, subscription and
customer detail views show a prominent `Pending plan change` badge, and the
subscription lifecycle owner refuses cancellation of the live subscription.
Operators must cancel the pending request first; the UI explains that ordering
instead of offering a second, replacement subscription as a workaround.

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

## OLT Operational Health Contract

- Audience and task: NOC staff compare OLTs in the inventory table and inspect
  one OLT without receiving contradictory operational answers.
- Authority: `network.device_state` owns the binary result and verifier-reason
  classification. The table and detail view consume that owner; templates do
  not infer health from stored booleans.
- First viewport: administrative lifecycle remains separate from the shared
  Working/Not working badge. Native OLT poll, ping, and SNMP evidence show
  Passed, Failed, Expired/Not current, Not checked, or Disabled with an
  observation timestamp where available.
- State semantics: a fresh successful native OLT poll is positive operational
  evidence. A linked monitoring row is fallback evidence only while active and
  current; historical successful probes on an inactive or stale row never
  certify present operation. Evidence freshness states explain the binary
  result and are not additional device states.
- Responsive behavior: evidence badges wrap without horizontal scrolling;
  labels and text communicate meaning independently of color, and timestamps
  remain available in badge tooltips.


## Personal Staff Notification Bell Contract

- Audience and task: authenticated staff need to see pending personal work and
  open the exact assigned entity without first opening the bell menu.
- Authority: `communications.staff_notification_read_state` owns the typed
  personal menu, unread count, and mark-read transition.
  `communications.staff_notifications` owns source-linked inbox materialization;
  the assigning business owner supplies the exact internal detail target.
- Initial and fresh state: every admin page loads the personal unread projection
  immediately and refreshes it at most 30 seconds later while the page remains
  open. Global delivery-queue totals never drive the personal red dot.
- Navigation: opening an item is scoped to the authenticated `SystemUser`, marks
  only that row read, and follows its stored target. A legacy ticket/project
  assignment whose stored target is only `/admin` is repaired once from an
  unambiguous canonical entity reference before navigation. Missing,
  ambiguous, or cross-user items fail closed without exposing another target.
- States and accessibility: zero unread removes the red dot; non-zero unread
  supplies an accessible count. Empty, loading, unread, read, and unauthorized
  states remain distinct, and notification identity is never communicated by
  color alone inside the menu.

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
  the server-owned deposit amount; `sales.quote_payment_review` owns the staff
  decision for the exact commercial snapshot; `financial.payment_routing` resolves
  Paystack availability; established invoice, intent, payment-verification, and
  Quote-acceptance owners retain every financial transition. The route and
  template are adapters.
- First viewport: quotation identity, expiry, authoritative currency and
  deposit amount, payment-review status and explanation, and—only after current
  staff approval—one Paystack action.
- GET state: authentication, ownership, active status, expiry, paid state,
  current staff approval, positive deposit, and Paystack availability are checked without creating an
  invoice or payment intent. Missing or unauthorized quotations render the same
  not-found state.
- Review state: Draft/Sent Quotes without a current approval show `Awaiting
  staff review` and no payment action. Approved Quotes show `Approved — Payment
  required`. Rejected Quotes show the owner-supplied rejection message. Mobile
  must consume `can_pay_deposit`; it must not infer payment eligibility from
  Quote status or deposit amount.
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
  authoritative full count of matching previous active and resolved
  conversations. History spans different endpoints only through an exact
  Subscriber, reviewed Party contact-point, or reviewed Reseller relationship;
  otherwise it is restricted to the exact normalized inbound endpoint and, for
  provider-scoped social identifiers, the same provider account scope.
  Ambiguous identity evidence renders `Not calculated`.
  The bounded newest-first list shows endpoint, channel, status, and last
  activity and routes each row to the exact prior Inbox conversation.
  Assignment does not narrow this customer history.

## Inbox Email Recipient And Copy Contract

- Authority: `communications.team_inbox_commands` validates reply copy-recipient
  input, `communications.team_inbox_outbound_intents` owns the durable delivery
  attempt, and `communications.team_inbox_projection` owns the staff recipient
  presentation. The route and templates are adapters.
- The reply composer exposes optional CC and BCC only for email conversations.
  The control uses a native disclosure so its visibility and operation do not
  depend on a particular browser JavaScript cache. Authenticated Inbox workspace
  and conversation HTML is private and non-cacheable.
  Invalid, ambiguous, or over-limit recipient input blocks the send; non-email
  channels reject copy recipients.
- Each email message in the permission-scoped staff thread renders all
  recorded From, To, CC, and BCC addresses. Unknown lists remain absent rather
  than being inferred from conversation identity.
- BCC is internal delivery evidence. It is included in the SMTP envelope but
  never in MIME headers or customer-facing projections.
- Mobile layouts wrap long addresses without hiding recipients or displacing
  the message body and primary Send action.

## Team Inbox Queue Position Contract

- Authority: `communications.team_inbox_routing` owns queue membership,
  admission sequence, current visible position, strict per-team FIFO,
  assignment capacity and promotion. Templates and routes are adapters.
- Staff and customer surfaces label only the live team-scoped rank as
  **Position**. The durable admission sequence is ordering evidence and must
  not be presented as the customer's current position.
- Normal manual and self-assignment of a queued conversation may select only
  the queue head and an eligible agent with capacity. A rejection must explain
  that an older conversation or capacity limit prevents the action; the UI
  must not imply that assignment succeeded.
- After AI control has ended, the first eligible human reply to an unassigned
  conversation atomically claims it for that agent and sends in one owner
  transaction. The claim obeys the same team membership, presence, capacity,
  and FIFO rules as explicit self-assignment. It may also replace a different
  owner only when the routing owner's locked presence evidence resolves that
  owner to `offline`, including missing or stale online evidence. Online,
  away, and on-break owners remain protected. A simultaneous or later reply
  against a protected assignment sends nothing and returns the conflict message
  **This conversation is currently assigned to [agent].** The composer presents
  that message without implying that its draft was sent.
- Admin → System → Settings → Comms exposes **Default active Inbox
  conversations per agent** with range 1–100 and default 10. Per-agent backend
  overrides are not presented as though they are editable when no Admin writer
  exists. The Inbox Manager Dashboard links authorized settings operators to
  that exact control, which explains that 10 is the default rather than the
  maximum and provides an adjacent **Save Inbox capacity** action.
- While the authenticated Inbox workspace is visible, it supplies a
  best-effort agent-presence heartbeat at five-minute intervals and immediately
  when visibility resumes. The routing owner may use this only to refresh
  selected online state; explicit away, on-break, and offline choices remain
  authoritative.
- Queue heartbeats are off by default. If enabled in AI intake policy they are
  clearly identified as reassurance, use different copy from a position
  update, and never repeat the current position.

## Agent Performance Analytics Page Contract

- Audience and task: CX/support leaders compare agent workload, resolution, and
  responsiveness; the personal variant gives the signed-in agent the same
  evidence restricted to their identity.
- Authority: `ui.crm_operational_reports` owns the typed report projection;
  Team Inbox assignment, message, status-transition, team, and staff-identity
  owners retain their underlying facts. Routes and templates are adapters.
- First viewport: current Africa/Lagos month by default, Today/Current
  week/Custom controls, active service-team filter, agent search on the
  administrative view, page size, export, and stable lazy-loading placeholders.
- Metrics and table: Active member agents, agents with activity, Assigned chats,
  Resolved chats, Active now, Avg first response, and nullable SLA adherence
  precede a compact Agent, Team, Assigned, Resolved, Active now, Avg first
  response, and Status/score table. Without configured SLA, existing workload
  and timing metrics remain visible and SLA is explicitly not scored.
- Agent names link to an admin detail report that preserves the originating
  period and team filter. The detail and signed-in-only My Performance views
  add average resolution, SLA met/breached evidence, needs-attention indicators,
  multi-team breakdown, and a bounded conversation evidence table.
- The dedicated Inbox Performance page's Agent Load table uses the same staff
  display-name fallback. Its internal person UUID is never rendered as the
  Agent value.
- Cohort semantics: inclusive local dates become UTC half-open bounds.
  Assignment, resolution, and first-response facts use their authoritative
  event times. Active now includes only current valid active service-team-member
  assignments; legacy unmatched rows are excluded without mutation. Filters,
  summary, pagination, personal scope, detail links, and CSV use one typed query
  contract.
- States: loading, empty, timing unavailable, read failure with retry,
  unauthorized, and missing personal identity are distinct. Read failure never
  displays cached or estimated values.
- Responsive behavior: the filter rail wraps, KPI cards stack, and the
  seven-column table remains horizontally accessible without hiding agent
  identity or metric meaning.
## Inbox Private Note Mention Contract

- Authority: `communications.team_inbox_commands` validates the active
  conversation, exact staff identifiers, and team visibility, then writes the
  private note. `communications.staff_notifications` stages the personal Inbox
  and email notices. `communications.nextcloud_talk_staff` owns optional Talk
  admission and delivery. Routes, templates, and browser code are adapters.
- Typing `@` exposes active colleagues who can access the conversation, including
  the initial unfiltered list. The list is appended to the document viewport,
  opens above or below the composer based on available space, and remains usable
  by keyboard, pointer, touch, zoom, and the mobile visual viewport.
- A selected mention retains its stable system-user ID. Removing its visible
  token or its chip removes the submitted recipient; the server revalidates every
  ID and rejects stale or inaccessible recipients.
- Saving a note stages one idempotent Inbox notice and one email for each
  mentioned colleague other than the author. When enabled, Talk staging runs as
  an optional owner savepoint and cannot roll back the note or other notices.
- Notification copy never includes the private note or customer data. Its link
  marks the personal Inbox item read, opens the exact conversation, scrolls to
  the note, and provides a temporary accessible highlight. Mentioning never
  assigns, transfers, or changes conversation status.

## Inbox AI Ownership Contract

- Authority: `ai.intake` owns active AI-session state. Team Inbox projection
  consumes that typed fact and supplies `control_owner`, `ai_session_state`,
  `can_take_over`, action booleans, and a denial reason; templates never infer
  ownership from lifecycle status or metadata.
- The default All/Actionable view excludes AI-owned work. AI Intake is a
  separate read-only view with an `AI handling` or `Waiting on customer` badge;
  Queue contains only durable active queue entries, and History preserves
  lifecycle review.
- While AI owns the thread, Reply, Private Note, assignment, status/workflow,
  ticket, macro, and bulk controls are absent or disabled with an explanation.
  Authorized operators receive one explicit `Take Over Conversation` control
  with confirmation and expected-session evidence. Takeover is available to any
  active staff actor with the required permissions regardless of team
  membership, presence, capacity, FIFO position, or an existing human
  assignment. The control submits the active primary team, defaults the sole
  active team, or requires an explicit team selection when several are active;
  team selection supplies routing and audit attribution rather than operator
  eligibility.
- UI gating is presentation only. Every mutation rechecks ownership at its
  backend owner and returns the stable AI-owned conflict; a normal reply never
  becomes implicit takeover. Reply auto-claim runs only after the AI-owned check
  succeeds and rechecks AI authority under the same conversation lock used for
  the human claim. AI handoff or explicit takeover must end AI control and
  cancel pending AI output before a reply can create human ownership.

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

## Admin Work-Order Expense Entry Page Contract

- Audience and task: authenticated staff with read access to the exact work order
  can track their own work-order expense claims and can create one after a
  technician has been assigned. The requester does not need to be that technician.
- Authority: `ui.work_order_expense_projection` owns the form, validation
  presentation, requester-owned list, action eligibility, and ERP delivery
  labels. `operations.expense_requests` owns the atomic claim, selected local
  approver, masked destination snapshot, and durable ERP staging; ERP owns
  approver eligibility, bank identity, account verification, and reimbursement.
- First viewport: the work-order identity remains the page context. The expense
  card explains that approval and payment happen in ERP, shows the actor's
  existing claims, and always exposes one New Expense Claim action. The action
  opens the form when a technician is assigned and ERP categories are available;
  otherwise it remains visibly disabled with the authoritative reason, including
  `Assign a technician first.` for an unassigned work order.
- Mutation: the multipart POST is explicitly CSRF protected, read-scope guarded,
  and scoped to the work order in the URL. No work-order, requester, person,
  user, or requester email input is accepted. A stable client reference prevents
  double creation. The command owner rechecks current technician assignment while
  holding the work-order lock, so a direct or stale form submission fails closed.
  - Form: the submitter selects an ERP-eligible approver and either the masked
    ERP profile destination or editable one-expense beneficiary/bank/account
    details. The override is verified by ERP and never updates the profile or
    survives a failed redisplay as a raw account number. Purpose and expense
    date are required, currency defaults to NGN, notes
  are optional, and at least one stacked repeatable item remains. Items expose
  ERP category, description, positive amount, optional date/vendor/receipt
  URL or upload/notes, category receipt rules, and category maximums. Server
  validation is authoritative; the running total is browser assistance.
- States: locally submitted, pending delivery, delivered but awaiting ERP
  acceptance, accepted, approved, rejected with reason, paid, and sync
  unavailable/failed remain distinct. A sent outbox event is never labelled
  accepted by ERP.
- Responsive behavior: line items are stacked cards at every width, controls
  retain labels and text errors, totals name their currency, and add/remove and
  submit actions remain accessible without relying on colour.

## Field Work-Order Note Contract

- Audience and task: an assigned technician records an internal staff note or
  an external customer-history note and follows that exact note through mobile
  delivery.
- Authority: `operations.field_notes` owns note creation, assignment checks,
  attachment links, retry identity, and the committed output.
  `operations.work_orders` owns work-order and assignment facts. The mobile
  outbox is a durable delivery projection only.
- Mutation: the app creates one stable `client_ref` before enqueue and reuses it
  for every retry. Identical retries return the original note; reuse with
  changed content fails closed.
- States: **Note saved** means the API accepted the note. **Queued for sync**
  means the durable local request is pending. **Sync failed** means a permanent
  rejection or exhausted retry is retained for review. These states are never
  collapsed into a generic success message.
- Refresh behavior: server notes and non-sent local note requests merge by
  `client_ref`; refreshing or reopening a job cannot silently remove queued or
  failed evidence.
- Responsive behavior: visibility and delivery labels accompany the note text,
  do not rely on colour alone, and remain readable on the mobile first viewport.
- Staff web projection: after API acceptance, the same canonical note appears on
  the work-order detail page and, when authoritative native links exist, on its
  project-task and originating-ticket pages. Related-context entries name and
  link their work order. They are not copied into task or ticket comment stores.
- Staff privacy: internal and external-history labels remain visible on every
  staff projection. Customer publication is out of scope; neither field-note
  visibility value alone authorizes portal display. Attachment downloads require
  the same exact work-order read scope as the detail page.
- Freshness: staff pages read committed rows on every request. Device-local
  queued or failed notes do not appear until delivery succeeds; no UI may imply
  otherwise.
