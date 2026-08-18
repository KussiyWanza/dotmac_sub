# Team Inbox Source-of-Truth Completion

**Status:** Implemented
**Decision owner:** Customer experience platform
**Scope:** Native Team Inbox; email, WhatsApp, Meta social, chat-widget, and
fiber-website inquiry channels

## Decision

Team Inbox is the operational communications workspace. It owns conversation
identity, message chronology, operator collaboration, and the projections used
by its list and detail screens. Providers are transports. They do not decide
conversation, contact, ticket, assignment, escalation, read, or official
timeline state.

Inbox and Support remain separate workspaces and lifecycle owners. A
conversation may carry a reviewed ticket reference for context, but it does not
create, transition, assign, or append to the official Support ticket timeline.
`support.ticket_lifecycle` owns those decisions. This change does not approve a
combined Inbox/Support workspace.

## Owner inventory

| Concern | Canonical owner | Responsibility |
| --- | --- | --- |
| Inbound provider facts and deduplication | `communications.team_inbox_observations` | Commits one normalized, fingerprinted provider observation before consequences |
| Consequence coordination | `communications.team_inbox_processing` | Locks a committed observation and invokes the relevant participants once |
| Conversation identity and threading | `communications.team_inbox_threads` | Resolves provider message/thread identity and writes conversations/messages |
| Contact, subscriber, reseller, and reviewed context | `communications.team_inbox_contact_resolution` | Produces explicit matched, ambiguous, suppressed, or unmatched outcomes and owns reviewed links |
| Conversation-to-Lead provenance | `communications.conversation_lead_relationships` | Owns the durable, auditable, one-active-Lead-per-conversation relationship |
| Customer context drawer | `communications.team_inbox_contact_context` | Composes permission-scoped Party, Lead, Ticket, conversation, Project, and Task sections with typed availability |
| Profile and Lead action resolution | `communications.inbox_lead_actions` | Resolves and coordinates identity-aware actions without owning Party or Lead fields |
| Routing, assignment, escalation, and FIFO queue | `communications.team_inbox_routing` | Applies configured team, availability, permission, SLA, durable queue admission, and promotion policy |
| Inbox automation | `communications.team_inbox_automation` | Matches Inbox-scoped conversation triggers and coordinates ordered assign, auto-assign, and tag actions |
| Reply reminders | `communications.team_inbox_reply_reminders` | Owns configured first/repeat due times and queues internal agent notifications until a reply settles the schedule |
| Agent introductions | `communications.team_inbox_agent_introduction` | Owns per-agent templates and the chat-widget-only first-pickup auto-send decision |
| Conversation status | `communications.team_inbox_status` | Owns every status transition and its immutable evidence |
| Historical lifecycle reconstruction | `communications.team_inbox_audit_reconstruction` | Applies only reviewed, hash-bound, provenance-graded historical evidence |
| Lifecycle audit timeline and drift | `communications.team_inbox_audit_projection` | Combines immutable evidence, exposes coverage, and reports current-state drift |
| Operator read/unread state | `communications.team_inbox_operator_state` | Owns per-person monotonic read cursors and unread repair |
| Outbound communication intent | `communications.team_inbox_outbound_intents` | Stages the intent, notification outbox record, and Inbox attempt together |
| Meta free-form reply window | `communications.team_inbox_reply_window` | Determines WhatsApp, Facebook Messenger, and Instagram DM free-form reply eligibility from qualifying inbound customer message chronology |
| Provider receipts | `communications.team_inbox_delivery_receipts` | Applies timestamp-monotonic sent/delivered/read/failed projections |
| Operator mutations | `communications.team_inbox_commands` | Coordinates one typed owner transaction for replies and collaboration actions |
| Composer AI polish | `communications.team_inbox_ai_polish` | Coordinates review-only, context-aware polishing of unsent staff drafts through the existing Team Inbox projection and AI generation owner |
| Visitor chat mutations | `communications.team_inbox_widget` | Owns authenticated portal and anonymous fiber-site widget session, message, read, and satisfaction commands; anonymous identity is exact-match or Party-backed prospect with ambiguity held for review |
| List/detail/metrics/actions, media, and location presentation | `communications.team_inbox_projection` | Normalizes filters, sort and pagination, computes KPIs, unread and action eligibility, resolves safe inline-image versus download-only media presentation, and maps validated structured coordinates to Google Maps links |
| Repair jobs | `communications.team_inbox_maintenance` | Rebuilds media worklists, retries failed intents, and applies stale-conversation policy |
| Realtime | `communications.team_inbox_realtime` | Publishes best-effort projections only after commit; clients refetch on gaps |
| SMTP health evidence | `communications.team_inbox_health` | Marks only the exact synthetic Message-ID generated by the runtime |
| Email/Meta/widget delivery | Transport owners | Verify and normalize envelopes; never own Inbox or Support decisions |

Campaign materialization remains the flush-only
`communications.team_inbox_campaigns` participant under the campaign and
outbound-intent owners.

The Inbox **All** status filter is the active operational queue and excludes
resolved conversations. The explicit **Done** filter is the resolved-history
view.

## Inbound flow and idempotency

1. The adapter verifies the provider signature or SMTP envelope and reduces the
   payload to typed message, attachment, structured location, participant,
   channel, identity, occurrence-time, and provenance fields. A location fact
   preserves validated latitude and longitude plus optional place name and
   address; it is not treated as downloadable media.
2. `InboxProviderObservation` is committed using the unique
   `(provider, provider_account_scope, provider_event_id)` identity and a
   fingerprint of normalized evidence. Exact retries replay the observation;
   the same identity with different evidence fails closed.
3. A separate processing owner locks the observation. It resolves threading,
   contact and routing, then stores the consequence identity on the observation.
4. A processed observation is a no-op on retry. Existing message and thread
   constraints provide a second idempotency boundary.

The fiber website uses the same boundary through the signed
`communications.fiber_inquiry.receive.v1` Integration Platform capability.
The verified delivery receipt is recorded before the normalized
`fiber_website` provider observation, and the observation processing owner is
the only caller that may create its Conversation, Message, and optional
Party-first Lead consequence. Exact email and phone evidence may link one
existing Subscriber. No match creates a reusable prospect Party and Lead;
conflicting, ambiguous, or suppressed matches fail closed by creating the
conversation without a Subscriber or automatic Party/Lead and setting
`identity_review_required` for operator review.

Missing provider occurrence times use an explicit stable unknown-time sentinel
for fingerprinting; `recorded_at` still records admission time. Private raw
provider payloads are not copied into Inbox messages, events, logs, or
projections. The verified integration receipt remains the transport audit
record under its own retention contract.

Receipts are ordered by provider occurrence time and delivery rank. Older
`sent` or `delivered` receipts cannot regress a newer `delivered`, `read`, or
`failed` state. Exact receipt retries return the existing result. Only bounded
provider error codes are retained.

## Routing policy

`communications.team_inbox_routing` owns the routing policy for all inbound
channels. Mailbox routes map recipient addresses to teams through
`TeamInboxEmailRoute`. Push channels map provider/account scopes to a default
team through `TeamInboxChannelRoute`. AI intake may then override that default
through `TeamInboxAiRoute` when the inbound metadata carries a supported intent
and confidence at or above the configured threshold.

The channel route decides whether AI routing is allowed for that account. When
AI routing is disabled, or when no confident AI result is present, the channel
default or configured fallback team remains the primary team. The route rows
are routing policy only; provider credentials and SMTP listener secrets remain
owned by configuration and secret-management contracts.

When no eligible agent has capacity, routing records one durable queue entry
with a team-scoped monotonic admission position and entry timestamp. The
periodic promotion command locks the oldest entries and each target team before
rechecking live capacity, promotes only the oldest eligible conversation, and
durably settles invalid or already-assigned entries. The agent projection
derives current FIFO rank and an estimated wait from that ledger and the live
capacity snapshot; it never makes a routing decision.

Automatic assignment uses `inbox_team_round_robin_cursors`, one durable cursor
per service team. The routing owner locks the team and cursor, builds the
eligible online candidate list, skips inactive/offline/full agents, advances
the cursor only inside the assignment transaction, and records routing evidence
with candidate capacity details. The default capacity is ten active
conversations per agent unless `InboxAgentPresence.max_concurrent_conversations`
overrides it. Capacity counts active human assignments on `open`, human-owned
`pending`, and `snoozed` conversations while ownership remains active. It
excludes resolved conversations and unassigned AI-pending conversations.

Queue communication is also owned by Team Inbox routing. `inbox_queue_notifications`
records initial position notices, movement updates, fifteen-minute unchanged
heartbeats, handoff notices, dedupe keys, delivery outcome and outbound message
links. Customer-visible queue messages are sent only through Team Inbox
outbound intents and only for WhatsApp, Facebook Messenger and Instagram DM.
Queue messages never invent estimated wait times. Promotion, transfer,
resolution, cancellation or assignment stops further queue updates.

## Outbound flow

An operator reply command accepts one typed `ReplyCommand`. It performs pure and
provider-template preparation before acquiring the conversation row, then takes
a late PostgreSQL `NOWAIT` lock for the bounded database-only write phase. Under
that lock it rechecks active state and the stable per-conversation idempotency
key, then records the communication intent, durable notification/outbox row,
Inbox outbound-attempt projection, attachments, and macro consequence in one
owner transaction. Exact key retries replay the existing message; changed input
under the same key fails closed. SQLSTATE `55P03` rolls back completely and maps
to the retryable `communications.team_inbox_commands.conversation_busy` domain
error, never to an HTTP 500. Dispatch occurs after commit through the canonical
notification delivery point. SMTP, WhatsApp, and social integrations translate
the intent and later return normalized receipt observations; they cannot change
conversation or ticket lifecycle state.

AI Polish is outside outbound delivery. It reads the bounded Team Inbox reply
projection, labels customer and agent excerpts as untrusted quoted content,
applies configurable support voice and protected safety instructions, and
returns only a staff-reviewed composer suggestion. It excludes private notes,
DOB, gender, credentials, delivery receipts and audit events. It may infer a
temporary mood/tone for the current request, but that metadata is not written to
the conversation or customer profile. Accepting a suggestion updates only the
unsent browser composer; sending remains owned by `communications.team_inbox_commands`.

For operator replies, the committed outcome exposes the exact notification
UUID to the HTTP transport. The adapter schedules an after-response single-row
delivery task on the dedicated `notifications_immediate` worker queue, so broker
latency and long notification recovery sweeps do not hold the composer response
open. The periodic notification runner remains on `notifications` as the
recovery sweep when broker publication or the immediate worker is unavailable.
Immediate tasks and sweeps both lock and claim the exact
eligible outbox row before provider delivery, so concurrent wake-ups are safe
no-ops rather than duplicate sends. Immediate replies with no operator-supplied
schedule bypass automatic customer quiet hours. An explicit `send_after`
remains authoritative and is displayed as scheduled rather than as an
unexplained queued reply. Delivery changes publish only bounded
message/conversation/status invalidations after commit; the Inbox refetches the
authoritative projection and never treats realtime as delivery evidence.

For WhatsApp, Facebook Messenger, and Instagram DM free-form replies,
`communications.team_inbox_reply_window` is the backend policy owner. It derives
the open window from the latest qualifying inbound customer message on the
conversation chronology, never from staff replies, private notes, audit events,
receipts, scheduled attempts, AI drafts, or failed outbound rows. The outbound
intent owner rechecks the policy immediately before staging a provider-facing
free-form send. Approved WhatsApp template sends use the existing template
metadata path and do not reopen the free-form window unless a subsequent
qualifying inbound customer message is recorded.

Provider reply-window state is calculated, not stored as
`InboxConversation.status`. The queue projection may filter and badge Meta
conversations whose calculated state is `expired`; conversations with no
reliable qualifying inbound timestamp remain `unavailable` and are not included
in that filter. Workflow states such as open, pending, snoozed, and resolved
remain owned only by `communications.team_inbox_status`.

Operator-initiated conversations use the same command boundary. The opening
message retains approved WhatsApp template identity and submitted provider
variables, and uploaded attachments are staged against the new conversation
then bound only after the opening outbound intent succeeds. A failed opening
send rolls the conversation and staged attachment facts back together.
Temporary conversation-lock failures during attachment staging are classified
by `communications.team_inbox_commands` only after its transaction has rolled
back. The web adapter maps the typed retryable outcome to HTTP 409 with a short
retry hint; it never completes the database transaction itself.

The admin CRM-replication controls use these existing owners:

- A Facebook or Instagram parent comment is an inbound social-comment
  `InboxMessage` on its `InboxConversation`; accepted public replies are
  outbound messages on the same chronology. The command validates the stored
  provider comment/account identity and platform limit, then stages a social
  notification and attempt. The worker calls Meta after commit. Provider
  acceptance adds the external reply ID and delivered state; a safe failed
  attempt remains visible and is never projected as sent.
- A business-initiated WhatsApp conversation requires a server-listed approved
  template name and language. Numeric header/body/button inputs become explicit
  Meta components and stay on the intent/message metadata through delivery.
  Contact lookup prefers unique active canonical Party contact points. Until
  the reviewed Subscriber-to-Party migration is complete, it also reads unique
  active unbound Subscriber phone fields as an explicit compatibility source.
  A selected compatibility result carries the Subscriber identity separately;
  it never pretends that the Subscriber UUID is a Party UUID. Canonical contact
  points win over compatibility rows, shared normalized numbers are omitted as
  ambiguous, and manual numbers normalize using the selected country. The
  fallback is retired after the Party/contact convergence audit reaches zero.
- Email CC/BCC is limited to opening a conversation. The command validates,
  lowercases and deduplicates each list, then stores both on internal intent
  metadata. SMTP places CC in the MIME header, never emits a BCC header, and
  sends the primary, CC and BCC addresses in the envelope.
- Fiber-website inquiries are inbound-only. The projection and outbound owner
  explicitly reject replies until a reviewed reply transport and prospect
  destination policy are approved.

## Lifecycle audit evidence

Routing, status, and presence transitions append immutable native evidence in
the same transaction as current-state projection changes. Routing evidence is
authoritative for why an assignment ended; an assignment interval stores only
`ended_at` and `ended_by_event_id`, avoiding a second copy of the reason.
Queueing always appends a routing event even though it creates no person
assignment. Escalations are events rather than overwriteable conversation
metadata. Status and presence JSON histories are compatibility projections and
are not audit authority.

Native evidence records typed source and reason codes, actor identity when
known, occurrence and recording time, and a unique source identity. Automatic
routing additionally preserves the selected assignment outcome; candidate
decision evidence remains bounded and excludes message content and customer
identity.

Historical reconstruction is a separate reviewed reconciler. Preview scans
the complete eligible source set, emits explicit unknown exceptions, binds a
source watermark and canonical SHA-256, and performs no writes. Apply refuses
changed evidence, a mismatched hash, missing approval reference, or duplicate
source identity. Reconstructed events retain an evidence grade and
`historical_backfill` provenance. The process never invents an actor, reason,
queue interval, or assignment ending timestamp. See
`docs/runbooks/TEAM_INBOX_AUDIT_RECONSTRUCTION.md`.

## Derived state and repair

| Projection | Inputs | Canonical writer | Repair |
| --- | --- | --- | --- |
| Contact link | Conversation route plus reviewed Party/customer facts | contact-resolution owner | Revalidate/reapply the reviewed link; ambiguity remains explicit |
| Operator unread | Message chronology plus per-person read cursor | operator-state owner | Set-based grouped queries recompute the projection; `rebuild_operator_read_state` removes impossible cross-conversation cursors |
| Queue metrics and response cohorts | Conversation lifecycle, ordered message chronology, agent reply provenance/delivery, ticket handoff, assignment, and read state | projection query owner | Recompute on every query; no independent flag or counter is authoritative |
| Customer context drawer | Exact Party/Subscriber/Lead links plus permission-scoped owner queries | contact-context query owner | Recompute on drawer load; per-section failures remain explicit and retryable |
| Meta free-form reply-window eligibility | Conversation channel plus ordered qualifying inbound customer messages | reply-window policy owner | Recompute on every send attempt and detail projection; a new qualifying inbound customer message reopens the free-form path |
| Realtime envelope | Current committed Inbox projection | realtime transport | `rebuild_conversation_projection` republishes a snapshot; clients refetch |
| Media and failed worklists | Authoritative message/intent metadata | maintenance owner | Idempotent scheduled maintenance commands |
| Structured location card | Validated latitude/longitude and optional name/address on authoritative message attachment metadata | projection query owner | Recompute on every query; an invalid or legacy coordinate-less location is unavailable and never receives a media-content URL. `communications.team_inbox_maintenance.repair_whatsapp_locations` can restore an explicitly scoped historical message only when a processed `integration.inbox` receipt names that exact message and retains valid structured coordinates |
| Lifecycle audit timeline | Immutable routing/status events and assignment intervals | audit-projection owner | Recompute on query; findings identify status mismatch and missing assignment-end evidence |

Database reads remain authoritative when Redis realtime is unavailable or
stale. Realtime has no replay authority.

## Page contract

- Screen: `/admin/inbox` and `/admin/inbox/{conversation_id}`.
- The conversation HTMX response is a thread-only partial. The loaded workspace
  owns global navigation and sidebar statistics, so opening a conversation must
  not recompute that full-page context before displaying its messages.
- Audience and job: authorized support operators triage, understand, assign,
  reply, collaborate, and close communication work.
- Information owner: `communications.team_inbox_projection` returns the list
  definition, normalized filters, canonical URL, page bounds, KPIs, unread
  state, detail composition, action eligibility, and the typed browser
  presentation for authorized media content and structured locations.
- Detail projection includes owner-provided Meta reply-window state. Templates
  render countdown and expired-state actions from that projection only; browser
  timers are presentation helpers and do not authorize a send.
- Private notes are internal messages written by the operator command owner.
  Mention metadata stores stable system-user identifiers, and internal mention
  notifications use the existing notification owner with deterministic dedupe
  keys. Private notes and mentions never create provider delivery intents.
- Lifecycle activity appears as subtle inline system timeline entries ordered by
  occurrence time with messages. The template must distinguish system entries
  from customer, agent, and private-note messages and must not delete or rewrite
  historical audit evidence to change presentation.
- Media presentation uses the resolved response MIME type, not a filename or
  provider claim. JPEG, PNG, GIF, WebP, and AVIF may render inline; SVG,
  unknown, and non-image content remains download-only. The HTTP adapter maps
  that typed outcome to `Content-Disposition`, adds `nosniff`, and prevents
  private customer media from being cached.
- A valid structured location renders as a location card whose explicit action
  opens `https://www.google.com/maps/search/` with the preserved coordinates.
  The template consumes the typed projection and does not parse provider
  payloads or construct the URL. Missing or invalid coordinates render as an
  unavailable attachment without a link, preventing a false media download and
  its resulting 404.
- Historical repair is a manual, idempotent Celery redrive. Operators must pass
  one to one hundred exact conversation UUIDs to
  `app.tasks.team_inbox.repair_whatsapp_locations`; the owner locks the matching
  location assets and messages, correlates each to a processed verified webhook
  receipt through its recorded consequence message ID, and copies only the
  validated structured location. The outcome reports repaired,
  already-complete, missing-evidence, and scanned-receipt counts. It never
  guesses coordinates from customer identity or message timing.
- Contact display identity is a read-time projection. A bound canonical Party
  name outranks the legacy Subscriber name, which outranks provider-observed
  inbound names, conversation metadata names, and finally the channel address.
  A normal list or detail refetch is its idempotent rebuild path.
- Filters: search, status, channel, team, assignee, Unreplied, Needs Attention,
  AI handling, ticket handoff, activity window, contact resolution, priority,
  mute, snooze, open, unassigned, and unread.
- Pagination uses the projection owner's exact filtered total and compact page
  sequence. Conversation drill-down URLs preserve the active filters, sort,
  page size, and page number. A confirmed HTMX reply uses its exact message UUID
  to fetch one typed message fragment and one filter-aware queue-row fragment;
  it does not rebuild the complete timeline or queue. Non-HTMX mutation
  fallbacks return to the same queue location rather than resetting to page one.
- Advanced Service Team conditions use the shared JSON filter grammar, but the
  Inbox projection owns their typed allow-list and relationship semantics. The
  only advanced field is `InboxConversation.service_team_id`; `=`, `!=`, `in`,
  `not in`, `is empty`, and `is not empty` evaluate active
  `InboxConversationTeam` links. Negative operators use `NOT EXISTS`, so a
  conversation linked to both Billing and Support does not satisfy “not
  Billing.” The Service Team lifecycle owner supplies the active selector;
  malformed, unknown, or inactive team identifiers fail closed with
  `communications.team_inbox_projection.invalid_filter`.
- Response cohorts are derived from the ordered message history. `Unreplied`
  means the latest customer message has no earlier valid customer/agent
  exchange. `Needs Attention` means a customer message was followed by a
  successful human-agent reply and then a later customer follow-up without a
  subsequent successful human-agent reply.
- A submitted reply has an agent provenance identifier and a current delivery
  state of queued, sending, accepted, sent, delivered, read, or retried. Only
  provider-accepted `accepted`, `sent`, `delivered`, or `read` states establish
  a successful agent delivery. Queued, sending, retried, failed,
  scheduled, AI-intake, and explicitly no-response-required messages do not
  establish the prior agent reply.
- Outbound message bubbles resolve their saved `sent_by_person_id` against the
  canonical `SystemUser` identity at read time, including inactive staff, and
  expose a typed display name and initials. Legacy, automated, deleted, or
  malformed sender references fall back to `Support agent` / `AG`; the current
  viewer is never presented as the historical sender.
- Needs Attention excludes resolved, snoozed, inactive, ticketed, Facebook
  comment, and Instagram comment conversations. Direct Messenger and Instagram
  DM conversations remain eligible.
- The projection is transaction-current: it recomputes from authoritative
  conversation/message rows, delivery metadata, and ticket provenance on
  every read. There is no stored flag to drift and no event-specific repair
  path; a normal projection refetch is the idempotent rebuild.
- Sidebar filter refreshes are a typed, reduced composition of the same
  projection: they retain queue rows, counts, filter options, and selected-id
  highlighting while omitting conversation detail, manager-dashboard, and
  compose-template work that the sidebar does not render. The browser applies
  latest-request-wins semantics and pauses fallback polling during an active
  filter request so stale responses cannot replace a newer operator choice.
  Search, pagination, history, manual/realtime refresh, read-state refresh, and
  polling share that coordination boundary. This ordering is transport state
  only: the server projection remains authoritative for filter meaning, queue
  membership, counts, sorting, pagination, and canonical query normalization.
- Incremental message and queue-row fragments are also typed projections of the
  same database authority. Their message/conversation identifiers are lookup
  keys, not cache authority; a row that no longer matches the active filters is
  removed, and normal projection refetch remains the idempotent rebuild path.
  Browser drafts and presentation preferences remain non-authoritative local
  state; sent messages and queue rows are never browser cache authority.
  Fragment reads are bounded to 15 seconds so a lost transport cannot hold the
  workspace busy.
- Advanced conditions are canonicalized into the URL and saved views. A normal
  projection refetch is their idempotent rebuild path.
- Queue-row unread totals count inbound messages after the authenticated
  operator's authoritative read cursor. With no cursor, every timestamped
  inbound message in the conversation is unread; outbound and internal
  messages never contribute.
- Unread page totals, filters, and per-row message counts use grouped set-based
  queries with a fixed query budget. They must not restore correlated
  per-conversation `MAX(received_at)` or read-cursor subqueries. Migration
  `507_team_inbox_unread_query_indexes` adds the partial message chronology
  index `(conversation_id, received_at)` for timestamped inbound rows and the
  operator cursor index `(person_id, conversation_id, last_read_at)`.
- Sort: the typed allow-list in `InboxListSort`; unknown values fall back to
  newest activity first (`last_message_at` descending). Priority remains an
  explicit sort and filter, not the default queue ordering. Page size is
  restricted to the declared options.
- Actions: routes map permission and domain outcomes only. Templates render
  owner-provided eligibility and never reconstruct lifecycle rules. Operators
  may set their own availability to `online`, `away`, or `offline`; automatic
  conversation assignment selects only effectively-online agents and queues work
  at the team when no eligible agent is available.
- States: empty, no-results, permission/error redirect, best-effort realtime
  stale state, and normal loading follow
  `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`.
- Responsive behavior: the queue and conversation actions remain usable at
  narrow widths; desktop-only density must not hide the primary reply/read
  actions.
- Sales actions: the projection supplies owner-resolved Lead-form eligibility
  and plan-family catalogue options to the composer. Templates display those
  outcomes without independently deciding customer/contact identity or
  catalogue availability. See
  `docs/designs/INBOX_PLAN_CATALOGUE_SHARING.md`.
- Pending incomplete-task assignment gate: conversational AI routing keeps the
  existing Team Inbox assignment eligibility boundary and does not query
  project, provisioning, CRM, or scheduling task tables directly. The blocking
  rule still needs an approved source-of-truth decision naming the task domain
  that owns incomplete work, whether the restriction is global or team-scoped,
  the exact statuses that block assignment, whether the gate is settings-backed,
  and the typed eligibility query that task owner will expose. Until that
  decision is recorded, automatic assignment is limited to the existing online,
  active-team-membership and conversation-capacity checks.

## Schema and migration

Migration `404_team_inbox_sot_completion` adds the normalized provider
observation ledger and per-operator conversation read cursor. It is additive;
no legacy data is inferred or deleted. Deployment order is expand, deploy
writers/readers, verify parity and duplicate handling, then retire old paths.

Migration `445_social_comment_channels` additively admits the
Facebook and Instagram comment channels to the durable notification outbox.
Its PostgreSQL enum labels are intentionally retained on downgrade; deleting a
label in place is less safe than leaving an unused additive value.

Migration 445 is based on main migration 444. If main advances before this
slice is published, rebase it and update the down revision; production must
never receive parallel unreviewed heads.

Migration `507_team_inbox_unread_query_indexes` expands the read path with two
concurrently built PostgreSQL indexes. The set-based readers are correct before
and during index creation, so deployment does not require a flag or dual-read
fallback. Deployment schema verification must require both indexes to be ready
and valid before the release is accepted.

## Retired paths

- The `communications.team_inbox` catch-all and its campaign baseline entry.
- Route-owned list definition, filter normalization, pagination, KPI and detail
  composition.
- Raw provider payload copies in Inbox message metadata.
- Direct commits in Inbox services, scheduled tasks, SMTP probe verification,
  and authenticated chat brokering.
- FastAPI `HTTPException` from the widget and chat service layers.
- Realtime publication before the authoritative transaction commits.
