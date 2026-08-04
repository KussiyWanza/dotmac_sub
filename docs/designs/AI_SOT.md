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
   cleaning flow; that skeleton performs no dialogue or customer-data access.

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
controls channel/scope, confidence, one optional clarification turn, fallback
deadline and team, department overrides, custom instructions, and campaign
attribution exclusion. The admin contract refuses email and limits
clarification to one turn.

`app.services.ai_intake` owns classification only. Normalized inbound
processing calls it after provider/message deduplication and before final team
routing. It may see the latest inbound message, at most three bounded recent
messages, bounded tags, and custom instructions; customer content and obvious
credentials are redacted before `ai.gateway` is called. Raw prompts and
unredacted content are not stored in intake metadata.

At or above the configured threshold, validated intent/category/department
metadata is handed to `communications.team_inbox_routing`. Below threshold,
one approved generic clarification may be recorded when enabled. The
classifier does **not** send that text; the Team Inbox coordinator submits it
to `communications.team_inbox_outbound_intents`, which uses the normal durable
WhatsApp, Facebook Messenger, or Instagram Direct notification path. The
provider call remains asynchronous to webhook acknowledgement. A dedupe key
derived from the inbound message prevents a repeated delivery from creating a
second clarification. A second uncertain result, disabled clarification,
provider failure, invalid output, or a five-minute timeout takes the configured
fallback team or normal channel default. The scheduled Team Inbox maintenance
owner locks each conversation row and repairs stale `classifying` or
`awaiting_follow_up` state idempotently.

Inbound processing serializes one channel/thread with a PostgreSQL transaction
advisory lock, then locks an existing conversation row before reading or
replacing intake metadata. This prevents concurrent webhooks, or a webhook and
the recovery task, from losing the follow-up count, fallback deadline, or
delivery evidence.

The contact-data cleaning scaffold stores only a conversation-level
`ai_data_cleaning` state. Its enum begins at `idle` and records
`identify_pending` only when AI intake is enabled for the channel and the
conversation's existing `primary_service_team_id` exactly equals the configured
`data_cleaning_support_team_id`. It never infers Support from a team name or
legacy `team_type`, and it performs no subscriber lookup, contact read/write,
identity proofing, or AI dialogue.

Schema tension remains unresolved: `uq_ai_intake_configs_scope_key` permits only
one `AiIntakeConfig` row per `scope_key`, regardless of channel or future flow.
The classifier and data-cleaning scaffold therefore currently share one row and
one enablement lifecycle. Before data cleaning gains dialogue or independent
controls, the configuration identity must be redesigned explicitly rather than
creating a parallel settings owner.

Destination-team resolution remains owned by Team Inbox routing. The queue
service remains the only owner of enqueueing and permanent queue numbers, and
the FIFO dispatcher remains the only owner of individual agent assignment.
Intake cannot move an actively assigned conversation. Email continues through
the email receive path and does not use AI intake.

This enablement is separate from advisor controls. `ai.generation`, reply
draft, polish, voice transcription, and operator approval retain their own
default-off controls and never send automatically.

## Implemented extensions

- **Inbox reply draft and sentence polish.** The Team Inbox owner builds a
  bounded projection and the `inbox_analyst` advisor declares
  `customer_content`. Drafts and polish suggestions land in
  `AIInsight.structured_output`; neither action sends. An agent must accept or
  insert the text, and sending still calls `team_inbox_commands.reply()`.
- **Voice transcription.** `ai.voice_transcription` is a separate
  zero-retention provider transport governed by
  `docs/designs/VOICE_TRANSCRIPTION_DATA_PROTECTION.md`. It writes no AI or
  domain row and inserts returned text only into an unsent browser composer.

## Open work

- **`ai_handling`.** The inbox conversation flag is read by the queue filter,
  the projection counter and the admin surface, and written by nothing. It
  needs an owner or removal; the AI chat-support work should settle which.
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
