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

AI is **advisory**. It observes, it derives, it recommends. It never decides
domain state. Three owners:

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

An **advisor** is not an owner; it is a declaration. `AdvisorSpec` binds one
advisor key to one owned projection, the output contract it must satisfy, the
sensitivity of what it sends, and the control that switches it off. Adding AI
to a domain means registering a spec against a projection that already exists
— not writing a new reader.

## The consequence rule (the load-bearing invariant)

**An insight never mutates domain state.** Acting on a recommendation means
calling the domain's declared owner — `support.tickets`,
`operations.work_orders`, `communications.team_inbox` — which applies its own
guards, events, and audit. AI requests an outcome; the owner decides it.

Concretely: no module under `app/services/ai*` may construct or session-write
a non-AI ORM row. `tests/architecture/test_ai_boundaries.py` enforces this.
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

## What is not implemented

`AiIntakeConfig` (`app/models/ai_intake.py`) is a **CRM import for a feature
Sub does not have**: an AI that answers inbound conversations, routes them to
departments and escalates to a fallback team. Its fields say so —
`department_mappings`, `fallback_team_id`, `escalate_after_minutes`,
`exclude_campaign_attribution`. It has an admin CRUD API and no reader.

It is **not** the gate for advisors and must not be pressed into that role:
forcing a conversational-intake model into the advisory path would leave most
of its fields inert while implying they mean something. Advisors are gated by
the `ai.generation` control, a per-advisor setting key, and a daily token
budget.

Its `allow_followup_questions` and `max_clarification_turns` fields describe a
multi-turn agent that fetches more data mid-reasoning. That contradicts this
design: the moment AI fetches its own data, the boundary stops holding by
construction. Such an agent would not extend this document — it would replace
it, and requires its own architecture decision.

`AiIntakeConfig` therefore stays parked and unread, pending the AI
chat-support work. If that work does not adopt it, delete the model and its
API: an unenforced gate with an admin UI reads as protection and is not.

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
