# Inbox Lead Intake

**Owning service:** `sales.lead_intake`
(`app/services/sales/lead_intake.py`).

## Purpose

Lead intake lets an unknown prospect in a supported Meta Inbox conversation
submit the minimum identity and service-location details needed to create a
Party-first Lead. It does not create a Subscriber, activate service, or grant
marketing consent.

The owner controls three concerns:

1. immutable, versioned individual and organization form templates;
2. sales-specific invitation eligibility, expiry, delivery, and revocation
   after a shared customer-intake handoff;
3. atomic form conversion into Party, Lead, Inbox participant binding, Sales
   routing, internal note, audit evidence, and `lead.created` event.

`ai.intake` owns general customer classification, confidence, clarification,
fallback, and team selection. Inbox receivers, the AI gateway, public/admin
routes, templates, and Meta delivery workers are adapters. They cannot create
or update the Sales-owned records directly.

## Eligibility and rollout

Automatic invitation is fail-closed. It occurs only when all of the following
are true:

- `integration.lead_intake_auto_send_enabled` is explicitly enabled;
- exactly one individual and one organization template version are published;
- an active channel-specific or `any` `AiIntakeConfig` exists;
- the conversation is active, unresolved (`unmatched`), and has no Subscriber;
- the channel is WhatsApp, Facebook Messenger, or Instagram DM;
- the shared customer-intake owner hands off `new_connection` or
  `coverage_request` at or above the configured confidence threshold; and
- the customer type is individual or organization at the same threshold.

General ambiguity and provider/schema failure are handled before Sales by
`ai.intake`. Sales will not issue a form until the shared classification
contains a controlled individual or organization type above the threshold.

The database permits only one automatic invitation per conversation. Staff
with `crm:lead:write` may issue an invitation manually, revoke an active link,
and then issue a replacement.

## Template and token contract

Templates are drafts until published. Publishing retires the previous version
for that customer type, and published/retired versions are immutable so an
issued invitation always resolves to the copy and routing policy reviewed at
issuance. The invitation message must contain `{link}`.

Public links use a cryptographically random token. Only its SHA-256 digest is
stored. Tokens expire after the configured duration, bounded to 24 hours, and
completed or revoked tokens are rejected. Public responses are non-cacheable,
use no-referrer policy, require CSRF validation, reject unknown fields, and are
rate-limited by a hash of client address and token digest.

## Submission contract

Individual forms require full name, gender, date of birth, service address,
address confirmation, and privacy acknowledgement. Organization forms require
organization name, representative name, representative role, business/service
address, address confirmation, and privacy acknowledgement.

The browser supplies only selected coordinates. On save, the server reverse
geocodes them, requires country `NG`, and normalizes the state or FCT through
the canonical Nigerian-state normalizer. Submitted identity data is not logged
or copied into AI assessment rows.

Within one owner command, completion:

- creates a Person Party, or an Organization Party plus representative Person
  and `contact_for` relationship;
- adds the prospect role and exact channel contact point with unknown consent;
- creates a Party-first Lead with immutable `inbox_form` / `team_inbox` origin;
- binds only the provider-scoped Inbox participant endpoint that received the
  link;
- routes the conversation to the template's Sales service team;
- adds a PII-free internal note, audit evidence, and `lead.created` event; and
- marks the invitation completed and links the created records.

The owner never creates a Subscriber. Account conversion remains with
`sales.account_conversion` through the established Quote acceptance path.

## Delivery and repair

Invitation and completion messages use the canonical Team Inbox reply command.
WhatsApp uses its configured sender; Facebook Messenger and Instagram DM use
the provider account scope captured on the inbound message and the durable
notification queue. Delivery records never store the public token separately
from the outbound message body already required for transport.

Operators diagnose drift by comparing invitation completion links, immutable
Lead origin, exact participant binding, conversation Sales-team ownership, and
the `lead.created` event/audit record. Replaying a completed owner command
returns the same deterministic Party and Lead identities.

## Schema

Migration `470_inbox_lead_intake` follows the Meta-channel migration and adds
only Sales-owned template, assessment, and invitation records. General intake
state remains in the canonical Team Inbox metadata written by `ai.intake`; the
migration does not create a competing customer-classification table.
