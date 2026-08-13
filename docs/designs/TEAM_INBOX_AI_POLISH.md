# Team Inbox AI Polish

**Status:** implemented advisory extension
**Owner:** `communications.team_inbox_ai_polish`

## Purpose

AI Polish helps a staff member improve an unsent Team Inbox reply. It is a
writing assistant only. It never sends a message, changes conversation state,
assigns work, transfers a conversation, creates a customer-facing AI message, or
updates customer/subscriber data.

The web route is an adapter. The service owns conversation-level authorization,
safe context assembly, AI generation coordination, deterministic safety checks,
and the typed staff-facing result.

## Existing Infrastructure Reused

- Inbox composer UI and manual send workflow.
- `team_inbox_projection.build_ai_reply_projection` for bounded context.
- `ai.generation` / `intelligence_engine.advise` for generation.
- `ai.gateway` for provider/model selection, retry and fallback.
- Shared customer-content redaction before provider egress.
- `ai.insights` evidence path for provider/model metadata and generated output.
- Existing CSRF and `support:ticket:update` route permission.

No second LLM gateway, provider client, conversation reader, outbound sender,
message store, queue, or status owner is introduced.

## Context Included

The service passes a bounded report to the AI advisor:

- `CONVERSATION_METADATA`: channel, status, priority, subject, labels, assigned
  agent display, linked ticket summary, and whether the surface is public.
- `UNTRUSTED_CONVERSATION_EXCERPTS`: newest bounded customer and agent messages
  from the Team Inbox projection, labelled `CUSTOMER_MESSAGE` or
  `AGENT_MESSAGE`.
- `CURRENT_UNSENT_DRAFT`: the staff draft to rewrite.
- `CONFIGURABLE_BUSINESS_VOICE`: administrator-editable support voice setting.
- `CONFIGURABLE_CHANNEL_GUIDANCE`: administrator-editable channel guidance.
- `SAFETY_CONTEXT`: explicit flags recording that private notes, DOB, gender,
  credentials, and automatic sending are excluded.

## Context Excluded

AI Polish does not include private notes, internal comments, delivery/read
receipts, audit events, raw provider payloads, unrelated historical
conversations, DOB, gender, credentials, payment credentials, or subscriber
profile fields that are not part of the existing bounded Inbox projection.

Quoted message content is untrusted. Customer text cannot override protected
system instructions.

## Voice Ownership

Protected safety instructions live in the advisor prompt and are not editable by
normal administrators. Business wording is configurable through integration
settings:

- `inbox_ai_polish_business_voice`
- `inbox_ai_polish_channel_guidance`

Defaults are Dotmac-appropriate: business casual, empathetic, concise, clear
English for Nigerian ISP customers, no forced slang, no unsupported restoration,
refund, credit, payment, coverage, plan price, or account-action claims.

This shape is intentionally compatible with a future shared AI communication
policy for Conversational AI Intake, but it does not depend on that unmerged
schema.

## Mood Inference

Mood is inferred only for the current polish request and returned to staff as
temporary assistive metadata. It is not written to the customer, subscriber, or
conversation profile and cannot drive routing, discipline, eligibility, or
automation.

Allowed values are `frustrated`, `angry`, `anxious`, `confused`, `urgent`,
`appreciative`, `neutral`, and `uncertain`. The service defaults invalid or
missing values to `uncertain`.

## Fact Preservation And Safety

The service extracts protected factual tokens from the original draft, including
numbers, dates, times, amounts, percentages, URLs, email addresses, phone
numbers, ticket/reference IDs, account-like IDs and technical values. If the
suggestion changes or removes those tokens, the result is marked unsafe, the
original draft is returned, and staff see a warning.

Drafts and suggestions are scanned for risky wording:

- guaranteed restoration or resolution timing;
- unverified payment confirmation;
- unverified coverage confirmation;
- refund, credit, compensation or rebate promises;
- plan prices or installation-date promises;
- legal/NCC claims;
- requests for passwords, OTPs, PINs or credentials.

Public-comment suggestions are also checked for account-specific identifiers.
When detected, the suggestion is not marked ready to use.

## Failure Behaviour

The service fails closed with safe staff-facing errors for missing conversation,
access denial, unsupported channel, empty or excessive draft, disabled AI,
provider unavailability, token-budget exhaustion, and invalid AI output. Provider
details and credentials are never exposed to staff.

## Relationship To AI Draft And AI Intake

AI Draft writes a new proposed reply from the same bounded projection.
AI Polish rewrites the staff member's current unsent draft and preserves staff
intent.

Conversational AI Intake is customer-facing and needs AI message identity,
session lifecycle and Team Inbox handoff. AI Polish is staff-assisted and
review-only; it should share provider, redaction, support voice and policy
infrastructure where practical, but it must not send or own conversation
lifecycle.
