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

## Meta contact profile observations and repair

Facebook Messenger and Instagram DM webhooks provide scoped sender identifiers,
not reliable display names. The Meta transport retrieves the sender profile and
`communications.team_inbox_processing` records the returned name as provider
observation metadata. Canonical Party or Subscriber identity still takes
precedence in Inbox display.

`communications.team_inbox_maintenance` owns historical repair. Its operator
tool is preview-only by default, produces a digest of exact conversation targets,
and applies only when the same digest is supplied. Each write locks the
conversation and refuses to continue if its channel or sender identity changed.
Unavailable profiles remain unchanged instead of replacing an identifier with a
guessed name.

Failed Facebook and Instagram replies use a separate preview digest and retry
only the exact failed message IDs shown in that preview. The outbound owner
preserves the original failed attempt, creates the retry with provenance, and
enforces the retry ceiling; repair never rewrites a failed attempt into a false
success.

Inbox and Support remain separate workspaces and lifecycle owners. A
conversation may carry a reviewed ticket reference for context, but it does not
create, transition, assign, or append to the official Support ticket timeline.
`support.ticket_lifecycle` owns those decisions. This change does not approve a
combined Inbox/Support workspace.

## Owner inventory

| Concern | Canonical owner | Responsibility |
| --- | --- | --- |
| Inbound provider facts, deduplication, and identity-collision quarantine | `communications.team_inbox_observations` | Commits one normalized provider observation before consequences and durably quarantines conflicting SMTP candidates |
| Consequence coordination | `communications.team_inbox_processing` | Locks a committed observation and invokes the relevant participants once |
| Conversation identity and threading | `communications.team_inbox_threads` | Resolves provider message/thread identity and writes conversations/messages |
| Contact, subscriber, reseller, and reviewed context | `communications.team_inbox_contact_resolution` | Produces explicit matched, ambiguous, suppressed, or unmatched outcomes, owns reviewed links, and projects bounded lazy Customer link options |
| Conversation-to-Lead provenance | `communications.conversation_lead_relationships` | Owns the durable, auditable, one-active-Lead-per-conversation relationship |
| Customer context drawer | `communications.team_inbox_contact_context` | Composes permission-scoped Party, Lead, Ticket, conversation, Project, and Task sections with typed availability |
| Profile and Lead action resolution | `communications.inbox_lead_actions` | Resolves and coordinates identity-aware actions without owning Party or Lead fields |
| Customer completion policy | `communications.team_inbox_customer_completion_policy` | Creates immutable Customer-only required-field versions and the active identity-guard decision; required fields are snapshotted by new conversations |
| Customer resolution readiness and Inbox profile completion | `communications.team_inbox_customer_completion` | Computes the central Customer-only gate and coordinates `customer.canonical_profile_patch` plus `party.registry`; Lead completeness is advisory |
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
| AI conversation control | `ai.intake` | Owns active AI-session state; an active session (`completed_at IS NULL`) is authoritative AI ownership and conversation metadata is only a repairable projection |
| Composer AI polish | `communications.team_inbox_ai_polish` | Coordinates review-only, context-aware polishing of unsent staff drafts through the existing Team Inbox projection and AI generation owner |
| Visitor chat mutations | `communications.team_inbox_widget` | Owns authenticated portal and anonymous fiber-site widget session, message, read, and satisfaction commands; anonymous identity is exact-match or Party-backed prospect with ambiguity held for review |
| List/detail/metrics/actions, media, and location presentation | `communications.team_inbox_projection` | Normalizes filters, sort and pagination, computes KPIs, unread and action eligibility, resolves safe inline-image versus download-only media presentation, and maps validated structured coordinates to Google Maps links |
| Performance and escalation reports | `communications.team_inbox_metrics` | Owns typed, bounded team and agent cohorts plus the current unresolved escalation projection; aggregates and paginates in the database |
| Manager AI analysis input | `communications.team_inbox_analysis_projection` | Recomputes scope-authorized selected-conversation, recent-queue, and period facts plus bounded evidence; it never writes Inbox state or lets AI query Inbox rows |
| Repair jobs | `communications.team_inbox_maintenance` | Rebuilds media worklists, retries failed intents, and applies stale-conversation policy |
| Realtime | `communications.team_inbox_realtime` | Publishes best-effort projections only after commit; clients refetch on gaps |
| SMTP health evidence | `communications.team_inbox_health` | Marks only the exact synthetic probe ID generated by the runtime, independent of provider Message-ID rewriting |
| Email/Meta/widget delivery | Transport owners | Verify and normalize envelopes; never own Inbox or Support decisions |

Campaign materialization remains the flush-only
`communications.team_inbox_campaigns` participant under the campaign and
outbound-intent owners.

The Inbox default and explicit **All / Actionable** view (`view=all`) is the
active, unresolved, human-actionable cohort. It excludes every conversation
with an authoritative active AI session. **AI Intake** (`view=ai_intake`) shows
active AI-owned conversations read-only, including `awaiting_customer`.
**Queue** (`view=queue`) shows only conversations with an active durable FIFO
queue entry and no active AI ownership. **History** (`view=history`) preserves
the prior all-lifecycle contract for review and search. Explicit filters narrow
their selected ownership/lifecycle cohort; they never make an AI-owned row
human-actionable. Historical cohorts use bounded pagination rather than scanning
the full result merely to render an exact total.

The stale-conversation policy may resolve an unassigned conversation only when
its latest non-internal message is a human agent reply older than the configured
threshold. An active assignment is authoritative unfinished-work evidence and
always excludes the conversation. The maintenance owner locks each candidate
conversation and rechecks assignment and message evidence in the owner
transaction before applying the audited status transition.

AI customer waiting is not an escalation signal. The AI session owns
`awaiting_customer`, the wait start, and a separate `expires_at` based on
`customer_wait_handoff_minutes` (10 minutes by default). Team Inbox maintenance
selects due sessions plus waits carrying a retired hours/expiry marker,
normalizes those deadlines from their authoritative wait start, and rejects
races with a newer customer reply or human takeover. At the deadline it records
a handoff summary, ends AI ownership, and enters the same authoritative routing
path used by other human work: FIFO order and available capacity decide whether
an eligible agent is assigned immediately or the conversation receives a durable
entry in the team's regular FIFO queue. A legacy row with no trustworthy wait
start receives a fresh 10-minute window before any consequence. Human routing
also occurs earlier
for a recorded explicit handoff reason such as a human request, unsupported
issue, policy boundary, required tool failure, or exhausted troubleshooting.

An active `AiIntakeSession` is the sole authority for AI control; the
conversation's `ai_handling` metadata is a rebuildable display projection with
transaction-current freshness and drift whenever it disagrees with the active
session query. While AI controls the conversation, ordinary human replies,
notes, assignment, status/workflow changes, ticket creation, macros, bulk
operations, workqueue claims, report escalation, and generic automation fail
closed with `communications.team_inbox_commands.ai_owned`. The typed AI handoff
provenance is the only exception for AI-authorized routing.

`TakeOverConversationCommand` is the only ordinary human transition out of AI
control. It locks and rechecks the expected conversation/session/state, stops
the session as `stopped_human_takeover`, clears customer wait, records actor,
reason, timestamps and prior state, and assigns the authorized active staff
actor. Explicit takeover deliberately bypasses ordinary assignment membership,
presence, capacity, FIFO, and existing-owner gates; the selected active service
team remains required as routing and audit attribution, but it is not an
operator-eligibility gate. The same transaction suppresses pending AI outbox
rows and stages projection and realtime effects. Assignment failure rolls the
whole operation back. A stable idempotency key replays the completed takeover;
stale session or state evidence returns a conflict. Delivery workers
independently revalidate the referenced active AI session immediately before
provider contact and cancel stale queued AI messages after takeover.
## Inbound flow and idempotency

1. The adapter verifies the provider signature or SMTP envelope and reduces the
   payload to typed message, attachment, structured location, participant,
   channel, identity, occurrence-time, and provenance fields. A location fact
   preserves validated latitude and longitude plus optional place name and
   address; it is not treated as downloadable media.
2. `InboxProviderObservation` is committed using the unique
   `(provider, provider_account_scope, provider_event_id)` identity. It retains
   an exact normalized-evidence fingerprint and a separately versioned semantic
   fingerprint. Semantic v3 retains the v2 explicit email field contract,
   treats legacy HTML-in-`body` and the current `html_body` plus readable-`body`
   shape as the same evidence, and excludes SMTP authentication and relay-hop
   evidence. It adds the normalized Fiber journey, attribution, address, map
   pin, and requested-plan fields so a delivery-ID replay with different
   acquisition evidence fails closed instead of reusing the first consequence.
   Transport fields remain in persisted normalized evidence but do not turn the
   same upstream message into a collision.
3. A semantic retry replays the observation. A true SMTP semantic mismatch is
   committed to `InboxProviderObservationCollision` with the first normalized
   candidate evidence, candidate fingerprints, bounded changed-field names,
   and retry count. It never overwrites or processes the admitted observation.
   SMTP returns success only after that quarantine transaction commits, ending
   deterministic redelivery. Pre-admission parsing and database failures remain
   SMTP-retryable because Sub has not saved the message. Once the observation
   is committed, processing failures are marked for internal replay/repair and
   SMTP returns success so the customer mail server does not resend the same
   saved message indefinitely. Other provider adapters retain fail-closed
   rejection until they deliberately adopt a transport disposition.
4. A separate processing owner locks the observation. It resolves threading,
   contact and routing, then stores the consequence identity on the observation.
5. A processed observation is a no-op on retry. Existing message and thread
   constraints provide a second idempotency boundary.

Rows written before semantic v3 are ratcheted lazily when an equivalent retry
arrives. This avoids a speculative bulk rewrite while still proving equivalence
from the stored normalized evidence before assigning the v3 fingerprint.

An inbound email that references an active thread joins it. If the exact
referenced thread is resolved, the message opens a new active conversation and
stores `continued_from_conversation_id` pointing to that resolved predecessor.
The predecessor stays resolved, and the timeline presents a direct link back to
it. No speculative backfill is performed for older rows without exact header
evidence.

The contact-context history query includes active and resolved conversations.
It broadens across endpoints only for an exact Subscriber relationship, a
reviewed Party contact-point binding, or a reviewed Reseller relationship.
Otherwise it matches the exact normalized inbound endpoint and, for
provider-scoped social identifiers, the same provider account scope. Ambiguous
evidence fails closed as `not_calculated` instead of merging customer records.

Party identity, Customer role, Lead role, and conversation context are separate
facts. Party contact points answer who owns an exact endpoint. Customer and
Lead records answer which business roles that Party holds. The active
conversation relationship selects whether this thread is Customer support or a
Lead/sales opportunity. A Party may hold both roles without being duplicated;
an active conversation-to-Lead relationship selects Lead context for that
thread, while a Subscriber-only thread remains Customer context.

Identity eligibility has three typed outcomes: one exact Party match, multiple
conflicting exact Party matches, or no exact match. Provider display names,
fuzzy names, and recently active Customer/Reseller/Organization rows are not
identity evidence. Bounded recent or text-search results may still be presented
as manual discovery suggestions, but they never change a no-match outcome or
block Create Lead. Multiple authoritative owners fail closed for human review;
the system never silently merges Parties.

An Inbox-origin Lead requires one prospect Party, Lead origin/provenance, an
active conversation-to-Lead relationship, and the exact observed inbound
endpoint bound to that Party in the same transaction. Email and phone/WhatsApp
use canonical normalized endpoints. Opaque Messenger and Instagram subjects
are keyed by provider, provider account, and external subject; the same subject
under another account is not the same identity. A missing trustworthy name may
remain the `Unknown` presentation fallback. Customer, Subscriber, billing,
installation, address, DOB, gender, and profile-completion facts are not Lead
resolution requirements.

Returning inbound resolves exact Party contact points, including Parties with
active Leads, before any new Lead is offered. One exact Party is reused;
multiple exact owners require review. Reusing a person does not itself decide
whether to reuse an existing opportunity or create another Lead: Sales SOT owns
that opportunity decision, and either outcome keeps the same Party.

The Existing Customer selector is lazy and has two explicit read modes. Focus
on an empty field requests at most eight conversation-derived likely matches;
there is no unrelated recently-updated-Customer fallback. Entering at least two
characters replaces those suggestions with at most eight active Customers
matched only from the entered name, email, phone, company/legal name, account
number, subscriber number, display name, or exact Customer UUID. Requests are
debounced and stale requests are cancelled. Neither mode writes identity.

An operator-selected Subscriber UUID is carried into the conversation command
as an explicit reviewed identity decision. When the sender is the Customer, a
reviewed manual contact link also repairs every
other active, unlinked conversation with the same normalized channel address;
it never overwrites a different Subscriber relationship. This makes the
customer conversation-history projection converge without matching names or
shared addresses in the browser. Before a reviewed link exists, the server may
narrow an exact normalized phone match with an exact normalized observed name;
a name mismatch remains ambiguous. Historical rows without reviewed or uniquely
resolved contact evidence remain unlinked for explicit reconciliation.

Contact-route writes serialize on the normalized channel endpoint before they
lock conversation and route rows. Reapplying the same target reuses the active
route and repairs only missing conversation projections. A reviewed different
target preserves the prior row as inactive, flushes that deactivation before
inserting its replacement, and leaves the partial unique index as the final
one-active-route arbiter. Route evidence that changes during review fails
closed and requires a fresh drawer.

When the sender represents someone else, the operator instead selects the exact
conversation participant, a represented existing Customer or Party-backed
Lead, and a required reason. The contact-resolution owner links a Customer only
to that conversation; the conversation-to-Lead owner records a selected Lead;
and the participant owner records `representative`. Neither path creates a
reusable contact route or performs historical repair, because one
representative endpoint may legitimately speak for different subjects in
different conversations.

Agent resolution uses the Customer-only completion gate defined in
`docs/designs/INBOX_CUSTOMER_COMPLETION_GATE.md`. Customer conversations must
satisfy their immutable snapshotted policy on canonical Customer/Party facts.
Lead profile gaps never participate in resolution readiness. Direct, bulk, and
macro resolution all enter the status owner and consume the same verdict.
AI intent classification and fact collection are observations, not canonical
identity. A conversational playbook may not resolve `new_connection` or
`coverage_request` while the conversation is still unidentified; it hands the
thread to the normal human routing path so the Lead/identity coordinator can
establish the minimum identity. Existing AI timeout and human-takeover behavior
is unchanged.

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

When no eligible agent has capacity, routing records one durable queue entry.
`InboxConversationQueueEntry.queue_position` is retained as the team-scoped
**admission sequence** for schema compatibility; it is durable ordering
evidence, not a customer-visible position. The **current visible position** is
the live rank of active `queued` entries for that team ordered by `entered_at
ASC, queue_position ASC`. The agent and customer projections derive that rank
from the queue ledger and never expose the admission sequence as a current
position.

Strict FIFO is serialized by the `ServiceTeam` row. Admission, normal manual
assignment, automatic assignment and promotion acquire that team lock before
examining the head. A promotion worker that cannot acquire the team lock skips
the whole team; it may not use `SKIP LOCKED` to take the team's second entry.
The recovery sweep selects one head cohort per queued team rather than one
global row batch, so a full team's backlog cannot hide another team's eligible
head. Resolution, assignment, cancellation, requeue and team transfer settle
or start the queue lifecycle transactionally. A conversation cannot remain an
active member of one team's queue while assigned, resolved, or owned by another
team.

Automatic assignment uses `inbox_team_round_robin_cursors`, one durable cursor
per service team. FIFO chooses the oldest customer first; round robin then
chooses the next eligible agent. The routing owner locks the team and cursor,
builds the eligible online candidate list, skips inactive/offline/stale/full
agents, serializes the final global capacity decision on the active
`SystemUser` row, creates the assignment, and only then advances the cursor.
The per-agent lock is intentionally not team-scoped because one agent may be a
member of several teams or channels. Normal manual, self, automation, workqueue
and promotion assignments use the same membership, presence and capacity gate;
there is no implicit force override and a queued non-head cannot be selected.
Reply auto-claim preserves a different owner whose effective presence is
`online`, `away`, or `on_break`. It may atomically replace that assignment only
when the routing owner locks the prior owner's presence evidence and resolves it
to `offline`; missing presence and online evidence older than the freshness
window fail closed to offline. The replacement is recorded as a distinct
routing reason in the same transaction as the reply and assignment transition.

An `online` presence is eligible only when its `last_seen_at` evidence is no
more than 30 minutes old; missing or stale presence fails closed as offline.
The authenticated Inbox workspace sends a best-effort heartbeat when it opens,
every five minutes while visible, and when a hidden tab becomes visible again.
The routing owner accepts that heartbeat only for an active `SystemUser`. It
creates missing online presence and refreshes selected online presence, but
never overrides an explicit `away`, `on_break`, or `offline` selection. A
hidden or closed workspace naturally becomes stale after the same 30-minute
window.
The default capacity is the `comms.inbox_agent_default_max_concurrent_conversations`
setting (default `10`, allowed range `1..100`) unless
`InboxAgentPresence.max_concurrent_conversations` supplies the existing
per-agent override. Administrators edit the default at **Admin → System →
Settings → Comms**, field **Default active Inbox conversations per agent**;
the field states its `1..100` range, identifies `10` as the default rather than
a maximum, and provides an adjacent save action. The Inbox Manager Dashboard
links authorized settings operators directly to that control. The canonical
settings writer invalidates the cache on commit and subsequent assignment
decisions consume the new value immediately. There is currently no Admin writer
for the per-agent override. Capacity counts active human
assignments on `open`, human-owned `pending`, and `snoozed` conversations while
ownership remains active. It excludes resolved and AI-owned conversations even
if legacy drift left an assignment projection behind. Default/actionable,
unassigned, pending-response, needs-response, unread-work, and manager workload
counts apply the same authoritative exclusion; AI Intake has its own count.
Explicit takeover is the intentional exception to membership, presence, FIFO,
capacity, and existing-owner assignment gates. Any active staff actor with both
takeover permissions may stop AI and acquire the conversation. The routing owner
still validates an active service team and records current availability as audit
evidence; bypassed eligibility never becomes the policy for ordinary manual or
automatic assignment.

Capacity-opening transitions schedule an idempotent promotion task after the
owning transaction commits. Agent return to eligible online presence,
resolution, reassignment and requeue therefore prompt promotion; the configured
60-second periodic sweep remains recovery rather than the sole trigger.

Successful staff session issuance submits one typed, flush-only sign-in command
to the routing owner in the same transaction. That command sets the signed-in
SystemUser's presence to `online`, clears any availability override left by the
prior session, refreshes `last_seen_at`, and records native transition evidence
when the effective state changes. The owner locks the active SystemUser row to
serialize concurrent first-presence creation for the same agent. Subscriber and
reseller sessions do not write agent presence. Operators may still select
another availability after sign-in; the existing 30-minute freshness and
assignment-capacity rules are unchanged.

Queue communication is also owned by Team Inbox routing.
`inbox_queue_notifications` records the admission generation, initial notice,
forward-only position updates, optional heartbeats, handoff notices,
deterministic dedupe keys, suppression reason, delivery outcome and outbound
message links. The queue entry durably records `last_notified_position`,
`last_position_notified_at` and `last_heartbeat_at`. Position keys have the
shape `queue-position:<entry>:generation:<generation>:<visible-position>`, so
the same position cannot be recreated through a later time window. Requeue
increments the admission generation and begins an independent notification
lifecycle.

The initial notice is sent once. A position update is sent only when the live
position moves forward; unchanged and worsening positions are suppressed, with
worsening movement recorded as structured evidence. Heartbeats are disabled by
the single policy default. When explicitly enabled they use a separate
non-position template, default to 30 minutes, and are suppressed until that
period has elapsed since the most recent position notice or heartbeat. Position
checks default to 10 minutes. Runtime, Admin form and policy fallback use these
same defaults.

Customer-visible queue messages are sent only through Team Inbox outbound
intents and only for WhatsApp, Facebook Messenger, Instagram DM, and the native
chat widget. Queue messages never invent estimated wait times. Promotion,
transfer, resolution, cancellation or assignment cancels pending intents for
that admission generation and stops future updates. Immediately before provider
contact, the notification dispatcher locks and revalidates the authoritative
conversation, active queue entry, assignment absence, generation, notification
kind and current visible position. A stale notice is durably suppressed without
contacting the provider.

## Outbound flow

An operator reply command accepts one typed `ReplyCommand`, including a typed
email copy-recipient value object for optional CC and BCC addresses. It performs pure and
provider-template preparation before acquiring assignment locks. The command
then asks `communications.team_inbox_routing` to atomically preserve or create
human ownership using the canonical team, agent-capacity, conversation, and
active-assignment lock order. Active AI ownership is rechecked under that lock
and fails closed; reply auto-claim is never an implicit AI takeover. An
unassigned conversation is claimed only by an eligible active team member with
capacity and, when queued, only at the FIFO head. An existing assignment to the
same agent is retained. An assignment to a different agent returns
`communications.team_inbox_commands.assigned_to_other` with that agent's display
name and sends nothing.

The bounded database-only write phase uses a PostgreSQL `NOWAIT` conversation
lock and rechecks active state. Its stable per-conversation idempotency key is
bound to the replying agent and includes normalized copy recipients in the
replay fingerprint. The transaction records the assignment, communication
intent, durable notification/outbox row, Inbox outbound-attempt projection,
attachments, and macro consequence together. Exact key retries by the owning
agent replay the existing message; changed input or a different agent under the
same key fails closed. SQLSTATE `55P03` rolls back completely and maps to the
retryable `communications.team_inbox_commands.conversation_busy` domain error,
never to an HTTP 500. Dispatch occurs after commit through the canonical
notification delivery point. SMTP, WhatsApp, and social integrations translate
the intent and later return normalized receipt observations; they cannot change

Before any queued AI Intake intent contacts a provider, the delivery worker
rechecks that the referenced AI session still exists, is incomplete, belongs to
the same active unresolved conversation, and has not reached
`stopped_human_takeover`. A failed recheck cancels the notification and marks the
Inbox attempt with a bounded suppression reason. This second admission check is
required even when takeover already canceled known queued rows because it closes
the enqueue/takeover/worker race.

For email, the thread owner derives one stable RFC `Message-ID` from the local
outbound Inbox message UUID before the intent is staged. It also derives
`In-Reply-To` and a bounded `References` chain from the newest exact inbound or
provider-accepted outbound message identity on that conversation. The outbound
intent persists those typed headers and SMTP serializes them unchanged; retries
reuse the same identity. A relay-generated identifier is never required for
local threading, and sender/subject similarity is never used as a fallback.

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
Email replies with Inbox attachments resolve those durable Inbox asset IDs only;
they never reinterpret Inbox display metadata as generic communication
attachments. The supported email attachment types include PDF, XLSX, and the
Team Inbox image types PNG, JPEG, GIF, and WebP.
Facebook Messenger and Instagram DM replies use the same private Inbox asset
identity and content boundary. The notification worker resolves each asset only
after the outbound intent commits, then the version-pinned `meta.social`
capability uploads the bytes to Meta's attachment API and sends the returned
attachment ID. It never exposes an authenticated Inbox media URL to Meta.
Instagram permits image, audio, and video assets; document attachments fail
closed before an intent is staged. Each accepted media leg and the optional
text leg records its provider message ID durably, and a retry skips those exact
completed legs rather than sending the customer a duplicate attachment.
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

For WhatsApp, expiry has an explicit workload consequence independent of
conversation status. When the latest qualifying customer inbound reaches 24
hours, the routing owner ends the active assignment with reason
`whatsapp_window_expired`, cancels the actionable queue entry, disables reply
reminders, and preserves all assignment, queue, message, Customer, Lead, and
Party history. The thread remains expired, unresolved, and unassigned. It is
excluded from FIFO, queue notifications, automatic assignment, agent capacity,
and stale auto-resolution. An authorized operator may resolve it internally
without assigning it, using an audited resolution reason; that action sends no
provider message, creates no assignment, and does not restart AI or reopen the
window. Only a qualifying new customer inbound opens a new window and re-enters
normal identity and routing policy. Templates, staff/AI/system outbound, and
delivery/read receipts do not extend the window.

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
- Email CC/BCC is available when opening a conversation and when replying to an
  existing email thread. The command validates, lowercases and deduplicates each
  list, rejects copy recipients on non-email channels, and stores both on
  internal intent metadata. SMTP places CC in the MIME header, never emits a BCC
  header, and sends the primary, CC and BCC addresses in the envelope. The
  permission-scoped staff projection shows From, To, CC and BCC for each email
  message; customer-facing rendering never exposes BCC. The staff reply control
  is a native browser disclosure, and the workspace and conversation fragment
  responses are private and non-cacheable so an older browser response or script
  cannot selectively remove the current action. Conversation-fragment request
  URLs carry the deployed presentation revision so browsers with a pre-fix
  cached fragment must request the current markup after deployment.
- Fiber-website inquiries are inbound-only. `fiber-contact-v1` keeps its legacy
  identity/Lead consequence. `fiber-coverage-v1` records the verified inquiry,
  creates an attributed Lead before an optional PostGIS feasibility check, and
  returns only a customer-safe coverage class. Exact reviewed Subscriber
  identity creates a new linked Lead; ambiguous or Party-less identity records
  review evidence and fails closed. Neither contract enables replies: the
  projection and outbound owner reject them until a reviewed reply transport
  and prospect destination policy are approved.

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
| Contact link | Conversation route plus reviewed Party/customer facts | contact-resolution owner | Revalidate/reapply a reviewed link, which repairs active unlinked threads on that exact normalized route; use the digest-bound `repair_team_inbox_subscriber_links` operator workflow for existing uniquely resolved routes; ambiguity remains explicit |
| Represented Customer association | Exact active conversation participant, selected active Customer, authenticated operator, and required review reason | contact-resolution owner with participant-owner classification | Reapply the reviewed command to the same conversation, participant, and Customer; it is idempotent and never widens to another conversation or creates a global contact route |
| Represented Lead association | Exact active conversation participant, selected active Party-backed Lead, authenticated operator, and required review reason | conversation-to-Lead owner with participant-owner classification | Reapply the reviewed command to the same conversation, participant, and Lead; the active link is idempotent and never creates a Lead or global contact route |
| Operator unread | Message chronology plus per-person read cursor | operator-state owner | Set-based grouped queries recompute the projection; `rebuild_operator_read_state` removes impossible cross-conversation cursors |
| Queue metrics and response cohorts | Conversation lifecycle, ordered message chronology, canonical Meta reply-window state, agent reply provenance/delivery, ticket handoff, assignment, and read state | projection query owner | Recompute on every query; expired WhatsApp is not actionable work and no independent flag or counter is authoritative |
| Performance report cohorts | Conversation lifecycle, ordered message chronology, recorded sender provenance, assignments, team composition, and staff identity in the selected half-open UTC period | `communications.team_inbox_metrics` | Recompute in set-based queries on every request; defaults to the latest 30 days, rejects ranges over 366 days, and exposes the effective period in the typed outcome |
| Escalation report projection | Current unresolved conversation lifecycle, ordered scoped messages, active assignment, effective agent availability, and team SLA policy at the observation time | `communications.team_inbox_metrics` | Recompute on every request; SQL pagination and owner-supplied aggregate totals share the same candidate scope, so no stored escalation flag can drift |
| Manager AI period analysis | Authorized Workqueue scope, Inbox message chronology, immutable status transitions, and immutable routing events | analysis-projection owner | Recompute on each request; the cohort is conversations with a message, status transition, or routing event in the selected half-open UTC period; evidence is bounded and never becomes Inbox truth |
| Customer context drawer | Exact Party/Subscriber/Lead links plus permission-scoped owner queries | contact-context query owner | Recompute on drawer load; per-section failures remain explicit and retryable |
| Meta free-form reply-window eligibility | Conversation channel plus ordered qualifying inbound customer messages | reply-window policy owner | Recompute on every send attempt, assignment/capacity query, expiry sweep, and detail projection; a new qualifying inbound customer message reopens the free-form path |
| Expired WhatsApp assignment/queue convergence | Canonical reply-window decision, active assignment interval, actionable queue entry, and reply reminders | routing owner, invoked by maintenance or inbound receipt | Idempotent expiry sweep and dry-run-first stale-assignment repair end only stale active work; they never resolve a conversation or create identity/queue facts |
| Inbox Lead endpoint binding | Active conversation-to-Lead relationship, Lead Party, and exact observed inbound identity | Lead authoring/contact-resolution coordinator | Dry-run-first repair reports safe missing bindings and conflicts; apply binds only unambiguous same-Party evidence and never merges Parties |
| Realtime envelope | Current committed Inbox projection | realtime transport | `rebuild_conversation_projection` republishes a snapshot; clients refetch |
| Media and failed worklists | Authoritative message/intent metadata | maintenance owner | Idempotent scheduled maintenance commands |
| Structured location card | Validated latitude/longitude and optional name/address on authoritative message attachment metadata | projection query owner | Recompute on every query; an invalid or legacy coordinate-less location is unavailable and never receives a media-content URL. `communications.team_inbox_maintenance.repair_whatsapp_locations` can restore an explicitly scoped historical message only when a processed `integration.inbox` receipt names that exact message and retains valid structured coordinates |
| Lifecycle audit timeline | Immutable routing/status events and assignment intervals | audit-projection owner | Recompute on query; findings identify status mismatch and missing assignment-end evidence |

Database reads remain authoritative when Redis realtime is unavailable or
stale. Realtime has no replay authority.

## Page contract

### Inbox report pages

- Screens: `/admin/reports/inbox-performance`,
  `/admin/reports/inbox-escalations`, and
  `/admin/reports/operational/crm-performance`.
- `communications.team_inbox_metrics` accepts typed date, scope, SLA, limit,
  offset, and observation-time inputs. Routes do not derive report facts.
- Performance pages and exports use the same explicit cohort. The default is
  the latest 30 inclusive calendar days, represented internally as a half-open
  UTC period; a range greater than 366 days fails closed.
- Escalations are current unresolved work rather than an all-history cohort.
  The owner applies candidate filtering, totals, and pagination in SQL before
  the adapter renders rows. Message bodies and attachment payloads are never
  loaded for these aggregates.
- Agent personal scope is applied in the owner query before any report rows are
  read. A missing signed-in person identity returns no personal rows.
- CSV export reuses the same typed owner and effective filters. It may request
  all aggregate rows, but it never materializes raw conversation or message
  history in Python.

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
  Mention metadata stores stable system-user identifiers after the command owner
  revalidates active conversation-team visibility. The same transaction stages
  deterministic per-recipient in-app and email rows through
  `communications.staff_notifications`. Optional Nextcloud Talk staging uses
  `execute_owner_savepoint`; a staging failure is recorded on note metadata and
  cannot roll back the private note. Notification copy excludes note and customer
  content. Its personal Inbox-open link marks the notice read, then targets the
  exact conversation and message for browser scroll and highlight. The author is
  not notified. Mentions never assign, transfer, change status, or create a
  customer/provider delivery intent.
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
- Filters: search, status, provider reply-window expiry, channel, team, assignee,
  Unreplied, Needs Attention, AI handling, ticket handoff, activity window,
  contact resolution, priority, mute, snooze, open, unassigned, and unread. The
  AI handling count and its drill-down use the same unresolved queue cohort.
  Lifecycle, assignment/team, and channel are independent filter dimensions:
  changing one preserves the others, so combinations such as All + My Team +
  Email resolve as one intersected owner query.
- Pagination uses an exact filtered total for active queues and bounded
  next-page evidence for demand-loaded historical cohorts. Conversation
  drill-down URLs preserve the active filters, sort, page size, and page number.
  A confirmed HTMX reply uses its exact message UUID
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
  at the team when no eligible agent is available. The teammate picker renders
  the routing owner's typed active-count, capacity-limit, remaining-capacity,
  eligibility, and refusal reason. An online agent at capacity remains visible
  with the exact `active / maximum` load and is disabled rather than being
  offered as an assignable target.
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
