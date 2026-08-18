# Lifecycle and Communications Source of Truth

## Ownership

### Account lifecycle

- `subscriptions.status` is the canonical service lifecycle fact.
- `subscribers.lifecycle_override_*` is the canonical administrative account fact.
- `subscribers.status` and `subscribers.is_active` are materialized projections written only by `account_lifecycle.compute_account_status`.
- Collections owns the inputs that derive `delinquent`; callers cannot assign it.
- Terminal subscription transitions own enforcement-lock cleanup, add-on termination, service-IP release, billing adjustment, and lifecycle events.

Projection order is account override, active service (including collections state), suspended service, blocked/stopped service, pending service, disabled services, then other terminal services. Clearing an override re-runs this derivation.

### Communications

- `communication_intents` records why communication is requested, its audience root, class, channels, schedule, content, sender context, and dedupe key.
- `communication_eligibility` owns the existing `communication_suppressions`
  ledger and the single address/channel eligibility decision. Intent expansion
  consumes that owner; it does not maintain a second suppression model.
- `notifications` is the delivery outbox. Every customer-facing notification points to an intent and identifies its expanded audience.
- `notification_deliveries` and `notifications.status` own provider outcomes.
- `inbox_messages` and `campaign_recipients` are projections linked by `notification_id`; they do not invoke providers.
- `app.tasks.notifications` is the customer transport consumer. Operational escalation retains its separate durable delivery queue.
- Campaigns own audience, sequence, content, and a canonical sender-key request.
  Email delivery owns sender-key resolution and SMTP configuration; campaigns
  never store relay credentials or override transport configuration.
- `comms.campaign_processing_enabled` is an admission decision owned by
  campaigns. When it is closed, callers cannot create a scheduled campaign or
  move an existing campaign into `scheduled`. It never freezes a campaign or
  sequence that was already admitted.
- Periodic campaign and sequence tasks are permanent drainage. At migration
  cutover, a missing or false admission decision moves existing `scheduled`
  campaigns to explicit `paused` state with evidence; work already `sending`
  continues toward a terminal outcome.

The processing order is:

1. Persist intent or return an existing dedupe-key match.
2. Resolve subscriber or explicit unlinked recipient.
3. Enforce marketing consent and account status.
4. Resolve channels and durable suppression.
5. Expand active non-house reseller recipients when requested.
6. Create outbox rows and linked inbox/campaign projections.
7. Deliver asynchronously and project provider outcomes.

Disabled and canceled subscribers never receive customer communication. Their active reseller can still receive a transactional event concerning the subscriber. Marketing requires subscriber opt-in and is never sent to an unlinked contact without proven identity/consent.

### Surveys

- `communications.surveys` owns Survey content, lifecycle transitions,
  invitations, response validation, and aggregate response metrics.
- Creation always produces a draft. A public slug is routing metadata and does
  not confer public availability.
- Public and invitation response access requires both lifecycle `active` and
  `is_active=true`; expired or questionless Surveys fail closed.
- Ticket-closed and work-order-completed invitations consume committed owner
  events through the event dispatcher. Invitation dedupe is persisted before
  the existing communication-intent owner queues delivery.

## Migration 457

- Adds the native Survey lifecycle, typed trigger, public slug, creator,
  invitation, idempotency, and response-metric columns.
- Preserves legacy Surveys as drafts so deployment cannot accidentally expose
  or automatically distribute old records.
- Does not create invitations, responses, notifications, tickets, work orders,
  or projects during migration or initial Survey creation.

## Migration 411

- Retires scheduler enablement controls for provisioning-compensation retry,
  device-login projection, active-session reconciliation, FUP expiry cleanup,
  and campaign drainage.
- Makes those scheduled tasks permanent so durable work and derived security
  state cannot freeze behind an operational toggle.
- Converts the campaign processing setting to owner-level admission only.
- Removes scheduler database settings for broker and result-backend URLs;
  those remain explicit deployment transport configuration.
- Treats an absent campaign-admission row as closed and pauses existing
  scheduled campaigns before permanent drainage is enabled.

## Migration 277

- Adds explicit subscriber lifecycle override fields.
- Preserves non-`new` subscriptionless account states as migration overrides.
- Preserves terminal account/service conflicts as overrides for reconciliation.
- Adds durable intents and notification/inbox lineage. The suppression table is
  retained from migration 273 and is not recreated or owned by this migration.
- Backfills active legacy outbox rows (`queued`, `sending`, and retryable `failed`) one-to-one into intents.
- Backfills normalized email hard-bounce suppressions from communication logs and delivery records.

## Prohibited writes

- No module outside `account_lifecycle.py` assigns subscriber or subscription status.
- CRM-reported status is retained as source metadata and cannot overwrite Sub lifecycle truth.
- Campaign and inbox services cannot call email, SMS, push, or WhatsApp providers directly.
- A customer notification without an intent is wrapped into one by the notification owner before an outbox row is created.
- Delivery timing has one precedence rule: an explicit `send_at` is preserved;
  otherwise `immediate` delivery is due now and bypasses automatic quiet-hours
  deferral, while `normal` and `batch` customer delivery continue to respect
  quiet hours. The resolved timing source is persisted in notification metadata
  for operator diagnosis.
- The notification queue UI and health signals distinguish future-scheduled
  rows from due queued rows. Only due rows older than the configured stale
  threshold produce backlog findings.
