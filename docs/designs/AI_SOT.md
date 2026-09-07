# AI under the source-of-truth standard

Status: accepted, 2026-07-26. Supersedes the proposed design of 2026-07-16.

## What changed and why

The 2026-07-16 design named four owners: `ai.gateway` (transport),
`ai.personas` (resolver), `ai.insights` (canonical writer) and `ai.intake`
(policy gate). The persona idea did not survive contact with the standard,
and the implementation correctly refused it. This document ratifies what was
built and records the reasoning, so the map stops describing a system nobody
wrote.

**Personas are removed from the design.** A persona, as originally specified,
"builds bounded context from the owning domain's read models" — which means
the AI module queries domain models to assemble its own view. That is a
parallel derivation path sitting beside the projection the domain owner
already computes, and the standard forbids exactly that. CRM, which took the
idea literally, needed a `data_quality` scorer per persona precisely because
each re-derived its own context and then had to grade it.

Sub already owns its report projections. So the rule is inverted:

> **AI advises ON an owned projection. It never re-derives one.**

The caller — a surface that already owns and displays the projection — fetches
it and hands the dict to the engine. The engine issues no session query
against a domain model. The consequence is not merely tidier: the boundary in
`tests/architecture/test_ai_boundaries.py` holds **by construction rather than
by vigilance**, and the quality-scoring problem disappears, because a
projection the owner computed does not need grading by us.

## The ownership shape

AI advisor features are **advisory**. Customer-facing intake is the narrower
exception described below: it may classify a request and propose a destination
service team, but it never owns queueing or agent assignment. Four owners:

1. **`ai.gateway` — transport, not a decision system.** Talks to the LLM
   provider, resolves credentials through `secrets` (OpenBao) rather than
   settings rows, applies the sensitivity redaction described below, carries
   circuit-breaker and provider-health state, and records latency and token
   telemetry. The same species as a payment gateway or an SMS provider: an
   external system Sub calls. It holds no business rule and owns no domain
   state.
2. **`ai.generation` — the advisory port.** `advise()` takes an advisor key
   and the caller's owned projection, assembles the prompt, calls the
   gateway, parses the structured output, and persists through `ai.insights`.
   It reads nothing of its own. This is where personas would have gone, and
   the reason there is one port instead of N resolvers.
3. **`ai.insights` — the canonical writer** of derived AI state
   (`ai_operations`, the sole writer of `AIInsight`). Owns insight lifecycle:
   create, acknowledge, expire. Every generated insight lands here and nowhere
   else.
4. **`ai.intake` — the conversational classification owner.**
   `AiIntakeConfig` is its sole runtime policy source. It resolves the most
   specific enabled provider/account/channel scope, builds a bounded redacted
   projection, validates strict JSON against controlled intent and category
   registries, and returns route-ready metadata. It supports WhatsApp,
   Facebook Messenger, and Instagram messages only. It also evaluates the
   exact configured Support-team UUID gate for the reserved contact-data
   cleaning flow. Versioned policies may opt into the composable conversation
   engine, which persists structured operational state, uses only the approved
   tool catalogue, and still leaves routing, queueing and assignment with Team
   Inbox.

An **advisor** is not an owner; it is a declaration. `AdvisorSpec` binds one
advisor key to one owned projection, the output contract it must satisfy, the
sensitivity of what it sends, and the control that switches it off. Adding AI
to a domain means registering a spec against a projection that already exists
— not writing a new reader.

## The consequence rule (the load-bearing invariant)

**An advisor insight never mutates domain state.** Acting on a recommendation means
calling the domain's declared owner — `support.tickets`,
`operations.work_orders`, `communications.team_inbox` — which applies its own
guards, events, and audit. AI requests an outcome; the owner decides it.

Concretely: no module under `app/services/ai*` may construct or session-write
a non-AI ORM row. `tests/architecture/test_ai_boundaries.py` enforces this.
`ai.intake` returns a typed projection; the Inbox owner alone persists and
routes from it.
The failure it prevents is an LLM's suggestion silently becoming a transition
that bypassed its owner's rules — an unreviewable authority leak, and the
exact parallel decision path the standard forbids.

## Data leaving the estate

Every advisor declares an `input_sensitivity`, and the port redacts according
to it before the gateway sees the prompt:

| Sensitivity | Meaning | Treatment |
| --- | --- | --- |
| `aggregate` | Counts, rates, durations. No identifiers. | Sent as-is. |
| `staff_identifiable` | Aggregates carrying internal names — team, assignee. | Sent as-is; recorded as carrying staff names. |
| `customer_content` | Text a customer wrote, or their identifiers. | Redacted: emails, phone numbers and token-shaped strings are replaced before sending. |

This exists because the classes are genuinely different and one global switch
collapses them. `ticket_sla_advisor` sends breach counts and team names; an
inbox advisor would send what a subscriber typed. Before this, `redaction.py`
existed but nothing called it, so the sensitive class was protected by a
module that never ran.

Redaction is a coarse guard against obvious identifiers leaving, not a full
PII scrubber, and it is applied by the port rather than trusted to each
caller. An advisor that would send customer content must declare it; the
declaration is what turns redaction on.

The prompt and the projection contents are never audited or logged — only the
fact of generation, the advisor, the projection key, and provider telemetry.

## Customer-facing conversational intake

`AiIntakeConfig` (`app/models/ai_intake.py`) is the runtime configuration owner
for conversational intake. No enabled matching row means classification is
skipped and the existing channel route remains authoritative. A matching row
controls channel/scope, confidence, optional clarification turns, fallback
deadline and team, department overrides, custom instructions, and campaign
attribution exclusion. The admin contract refuses email and limits
clarification to one turn.

`app.services.ai_intake` owns typed message understanding and customer-response
composition. Eligibility remains provider-free. The session processor sends the
persisted selected inbound to the existing gateway once for classification and
fact extraction, then may send a separate safe bounded projection to the same
gateway to phrase the backend-selected next action. That projection contains the
latest customer statement, at most six role-aware customer-visible messages,
known and missing fact keys, question state, approved identity/monitoring
projections, playbook step, business tone, and approved ISP information.
Internal notes are excluded. Customer content and obvious credentials are
redacted before gateway calls; raw prompts and unredacted content are not stored
in intake metadata.

At or above the configured threshold, validated intent/category/department
metadata is available to the conversational engine. Below threshold, the same
engine first honors an explicit human request and otherwise chooses one useful
policy-constrained missing fact; low confidence is not a handoff action. The
response model may phrase only that approved action. A backend validator rejects
action/purpose mismatch, repeated or unapproved questions, repeated apology,
internal terms, unsupported promises, invented monitoring/outage/payment facts,
and unsupported diagnosis. Safe deterministic playbook/template wording is the
fallback. The Team Inbox coordinator submits accepted text to
`communications.team_inbox_outbound_intents`, which uses the normal durable
WhatsApp, Facebook Messenger, Instagram Direct, or native chat-widget delivery
path. The
provider call remains asynchronous to webhook acknowledgement. A dedupe key
derived from the inbound message prevents a repeated delivery from creating a
second response. Customer silence records `waiting_reason=awaiting_customer` and
does not route, assign, queue, or increment AI inability. `expires_at` is a
separate `customer_wait_expiry_hours` policy (72 hours by default, bounded to
24?720 hours). The scheduled Team Inbox maintenance owner locks only sessions
past that long-term expiry, rechecks newer inbound and human takeover, then
transitions the AI session to `expired` and the Inbox conversation to resolved.
It creates no handoff note or assignment. A newer customer reply resumes the
same graph in `collecting_intent`; legacy short-wait rows are extended onto the new
long-term lifecycle before any expiry consequence.

Inbound processing serializes one channel/thread with a PostgreSQL transaction
advisory lock, then locks an existing conversation row before reading or
replacing intake metadata. This prevents concurrent webhooks, or a webhook and
the recovery task, from losing the follow-up count, fallback deadline, or
delivery evidence.

The first persisted eligible inbound customer message is turn 1. Starting or
resuming AI intake is not a separate customer turn: welcome delivery is
idempotent and may occur immediately before processing the same persisted
inbound, but it never consumes, replaces, or skips that message. Duplicate
provider webhooks are suppressed by the inbound message path before the session
processor sees work; processor retries use the welcome and per-inbound AI
message dedupe keys and mark `processed_inbound:<message_id>` only after the
classification/engine path reaches a durable outcome.

The conversational intake extension stores durable lifecycle in
`ai_intake_sessions`. `ai_intake_configs` remains the compatibility row used by existing routes, but customer-visible AI messages attach to immutable
`ai_intake_policy_versions` so the display name, welcome message, editable
business instructions, approved ISP information, queue templates and data
cleanup policy are auditable after activation. Protected system, security,
privacy and ownership instructions stay code-owned and are not editable by
normal administrators.

An activated policy version can enable the composable conversational engine
with metadata owned by `ai.intake`. The engine persists structured operational
state in the active session metadata: current and previous intent, category,
confidence, subscriber/contact identity, permitted identifiers supplied by the
customer, corrected collected facts, missing facts, typed question/answer state,
issue acknowledgement, bounded customer statements, troubleshooting steps,
typed monitoring observations, tool results/latency, tool errors, escalation,
waiting/resolution reasons and counters. It does not store chain-of-thought.

`continue_classifier` is not an engine routing action. The graph must select a
policy-constrained question, guidance, approved read-only tool, wait, resolution,
or an explicit-reason handoff. `mark_resolved` produces the terminal AI
`resolved` state and delegates the authoritative Inbox status transition to
`communications.team_inbox_status`.

A media-only first inbound is persisted and its attachments are promoted before
the configured customer-visible media handoff notice and factual private note
are created. Team Inbox performs normal routing/assignment and AI ownership ends
without attempting customer lookup, monitoring, or attachment interpretation.
Captioned media with usable text follows normal intake.

The approved tool catalogue is backend-owned. Current tools are:

- `customer_lookup`: read-only lookup by a policy-permitted Portal/account ID,
  registered email or registered phone. It returns only support-relevant
  subscriber fields and explicit statuses: `found`, `not_found`, `ambiguous`,
  `unavailable` or `unauthorized`.
- `subscriber_monitoring`: read-only wrapper over the existing customer network
  context and ONT/RADIUS status projections. It reports bounded factual service
  state, live-session and equipment status where available. An unavailable
  result must not become a diagnosis or a handoff unless the activated policy's
  bounded `tool_failure_handoff_statuses` explicitly requires that status to
  hand off. Playbook conditions use `monitoring_status`, `radius_status`, and
  `ont_status` separately; customer-reported LOS and connectivity facts remain
  collected facts rather than monitoring observations.

Policy versions may select which identifiers can be requested, and in which
order. Customer identity prompts are asked one at a time, and the policy's
declared `permitted_identifiers` order is authoritative: it is preserved
exactly, de-duplicated by first occurrence, and unsupported values are dropped
without reordering the remaining ones. The engine never imposes a canonical
order over a declared one. When a policy declares no identifiers, the default
order is registered phone, then registered email, then Portal/account ID. AI
intake asks the next identifier only when the previous one is absent or did not
identify the account, and identification lookup attempts identifiers in that
same declared order, so prompting and lookup can never disagree. The UI cannot
create arbitrary tools or executable conditions. If a customer explicitly asks
for a human, AI intake records `human_requested=true`, stops troubleshooting and
requests handoff while preserving already collected facts.

Policy versions may define bounded first-line playbooks. A playbook matches an
intent and optional category, then runs configured steps in order: request one
missing field, provide configured guidance, invoke an approved read-only tool,
mark resolved, or request handoff. Playbook wording, including empathy or
acknowledgement text, is policy data curated from approved support patterns; the
engine supplies only generic fallback field prompts and does not hardcode a
category-specific conversation path.

Policy versions may opt into immediate handoff after classification through
`conversation_policy.handoff_after_classification`. The default is false for
newly saved conversational policies. A classifier result that requires an
approved follow-up question takes precedence over this option, so low-confidence
or otherwise incomplete classifications remain in the clarification path instead
of being escalated to Team Inbox routing immediately.

Policy versions may select `conversation_engine_mode=custom_v1` or
`conversation_engine_mode=langgraph_v1`. LangGraph is an orchestration layer
only: it hydrates the same `AiIntakeSession.metadata["conversation_state"]`,
runs a stable graph of policy-driven nodes, and returns the same typed
conversation decision contract used by the existing session processor. Dotmac
state remains authoritative; LangGraph checkpoints are not a business record
and are not used for routing, queueing, assignment, or customer identity.
During rollout, `langgraph_v1` is an optional runtime capability: publication
validation refuses LangGraph activation when the package is unavailable, and
`custom_v1` remains the deployable default until the server dependency is
installed deliberately.

The authoritative AI session lifecycle starts as
`eligible -> welcome_pending -> collecting_intent`. A customer-facing question
transitions to `awaiting_customer`; a reply resumes `collecting_intent`. The
terminal branches are `resolved`, `expired`, `completed`,
`stopped_human_takeover`, `fallback_escalated`, `failed`, and `ineligible`.
Explicit escalation uses `handoff_requested -> completed`. `queued` and
`assigned` are not AI states; they are Team Inbox routing outcomes and may
appear only as derived audit metadata.

The contact-data cleaning flow is independent and disabled by default for
production collection. AI may collect candidate values only when the
conversation is reliably linked to a directly managed residential
`user_type=customer` subscriber missing gender, date of birth, or both. AI never
updates subscriber rows. `customer.profile_cleanup` owns the bounded typed
eligibility projection and validates and saves
through a typed command, rechecks eligibility under lock, refuses reseller or
ambiguous identities, prevents overwriting existing values, and appends
`subscriber_field_verifications` evidence without storing DOB in AI metadata.

Destination-team resolution remains owned by Team Inbox routing. The queue
service remains the only owner of enqueueing and permanent queue numbers, and
the FIFO dispatcher remains the only owner of individual agent assignment.
Intake cannot move an actively assigned conversation. Email continues through
the email receive path and does not use AI intake.

This enablement is separate from advisor controls. `ai.generation`, reply
draft, polish, voice transcription, and operator approval retain their own
default-off controls and never send automatically.

## Implemented extensions

- **Inbox reply draft and context-aware sentence polish.** The Team Inbox owner
  builds a bounded projection and the `inbox_analyst` / `inbox_sentence_polish`
  advisors declare `customer_content`. `communications.team_inbox_ai_polish`
  coordinates object-level access, private-note exclusion, temporary mood/tone
  metadata, protected-fact preservation checks and unsupported-promise warnings.
  Drafts and polish suggestions land in `AIInsight.structured_output` under the
  existing one-hour polish retention; neither action sends. An agent must accept
  or insert the text, and sending still calls `team_inbox_commands.reply()`.
- **Voice transcription.** `ai.voice_transcription` is a separate
  zero-retention provider transport governed by
  `docs/designs/VOICE_TRANSCRIPTION_DATA_PROTECTION.md`. It writes no AI or
  domain row and inserts returned text only into an unsent browser composer.
- **Manager Inbox AI.** `app.services.team_inbox_manager_ai_chat` is a
  read-only advisory view behind `support:inbox_ai:read` and the
  `ai.generation` control. It consumes the owned
  `communications.team_inbox_analysis_projection` for a bounded Team Inbox
  conversation, recent queue, or period review. Period facts cover the full
  authorized cohort; qualitative AI review is limited to the explicitly
  reported evidence sample. It cannot assign,
  reply, close, refund, profile-update, or otherwise mutate a domain row.
- **Conversational AI intake.** WhatsApp, Facebook Messenger, Instagram DM,
  and explicitly scoped native Fiber, customer-portal, and reseller-portal
  chat widgets may enter `pending` UI state with an active
  `ai_intake_sessions` row. For a widget, policy
  matching occurs on the first persisted visitor message; without an active
  matching policy it remains on the normal human Team Inbox path. The AI
  sends through Team Inbox outbound only, uses `sender_type=ai` identity
  metadata, classifies intent, and requests handoff. Team Inbox remains the
  owner of routing, queueing, assignment and provider delivery. The admin
  composer writes versioned intent definitions, approved tool selections,
  typed troubleshooting rules, handoff templates and queue wording into
  `ai_intake_policy_versions`; activation validates canonical intent/category
  keys, active team references, supported tools, approved rule
  conditions/actions and template variables. Preview has two explicit modes:
  simulation, which uses deterministic mock tool results and performs no live
  customer or monitoring reads, and authorized read-only preview, which may use
  enabled read-only tools.

## Open work

### Support-safe conversational context

`communications.team_inbox_contact_resolution` owns the bounded customer
identity projection consumed by conversational AI. `network.radius_sessions`
owns its bounded RADIUS observation projection; `network.ont_runtime_status`
owns timestamped OLT observations and the ONT status owner derives effective
state through its approved helper; it is not a separate authoritative owner.
The support monitoring projection preserves RADIUS and effective-ONT
provenance rather than diagnosing their combination. AI and future LangGraph are read-only consumers of these DTOs: they do
not import customer, RADIUS, or ONT ORM models, choose support scope, or turn
no-data/unavailable observations into an offline diagnosis. Exact identifier
lookup remains restricted to the authoritative Inbox-linked Subscriber until a
separate authenticated support-directory contract is approved.

- **`ai_handling` projection repair.** `ai_intake_sessions` now owns the AI
  lifecycle. The conversation metadata flag is a rebuildable UI/filter
  projection and should gain a focused repair command if drift is observed.
- **Confidence floor.** `confidence_score` is captured on every insight and
  never used to decide whether to surface one.
- **Typed contracts for `ai.insights` and `ai.generation`.** `ai.gateway` is
  contracted; the other two remain in the shrink-only legacy manifest
  baseline, and clearing them is behaviour change rather than documentation.
  The registry requires any writer to declare an event contract, and
  `ai_operations` emits no events; it requires a service that manages its own
  transaction to route through `execute_owner_command`, and `advise()` commits
  directly. Both are worth doing — an insight generated is a fact other
  domains may want, and the commit boundary should look like every other
  owner's — but they are their own change, not a side effect of this one.
- **Provider boundary on the connector runtime.** The gateway resolves its own
  credentials and endpoints today. Moving it onto the installed-connector
  machinery would give it the same install, config-revision, secret and
  declared-egress path as WhatsApp and the payment gateway. That is a refactor
  of a working transport, not a prerequisite for anything above.

## Non-goals

- Voice-driven field extraction, automatic commands, or sending without agent
  review. Transcription is transport-only under the accepted voice
  data-protection contract.
- CRM-marketing advisors (`campaign_optimizer`, `customer_success`) — deferred
  until the marketing and sales domain lands in Sub, which
  `docs/designs/MARKETING_SALES_SOT.md` commits to. They are out of scope here,
  not abandoned; when they arrive they are advisors over an owned campaign or
  audience projection like any other, not ported personas.
- Any AI-initiated domain mutation. Not in this design, not later, without an
  explicit architecture decision replacing this document.
