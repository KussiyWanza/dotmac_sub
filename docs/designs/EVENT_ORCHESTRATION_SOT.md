# Event orchestration source of truth

`app.services.control_relationships` owns event execution policy. Handlers own
their business behavior and expose their event scope through the mapping or set
already used by that behavior. The dispatcher builds one executable plan from
those sources before invoking any handler.

## Modes

- **Fanout**: consequences are independent. A failure is recorded and retried,
  but it does not suppress other handlers.
- **Chain**: state handlers run first, customer communication runs after all
  required state outcomes, and external delivery runs after state and
  communication. Handlers in the same stage remain independent unless the
  registry declares a specific dependency.

`payment.received` and `invoice.overdue` are fanout events. Payment arrangement
progression, access restoration/suspension, customer receipts/reminders, and
external integrations are separately valuable consequences; one failure must
not suppress the others. A successful payment notification derives its stable
receipt reference and authenticated portal receipt URL from the persisted
payment; delivery is queued through communication intents and never runs inside
the financial transaction.

`prepaid_service.renewed` is the canonical, fanout customer outcome for one
funded service period. The renewal owner stages it in the same transaction as
the exact debit, entitlement and subscription paid-through advancement. Its
`renewed_through` value is copied from that owner result; notification and UI
consumers must not infer expiry from `payment.received`. A payment may settle
without renewing a service, while a scheduled renewal may use previously held
account credit without a new payment. A trigger payment ID is correlation, not
payment-source attribution for pooled account credit.

`account_credit.deposited` is a chain because its state consequences have a
strict order: ordinary payable invoices have already consumed the credit, the
prepaid-renewal owner may then fund one currently due service period, and only
after that result may access enforcement re-evaluate restoration. External
delivery follows both state handlers. This event does not make the payment
writer the owner of service duration or access.

Subscription transitions, usage exhaustion, service-order assignment, and
provisioning outcomes are chains. Activation and resume have an additional
in-stage rule: enforcement depends on provisioning so RADIUS/session work
cannot run before IP/service provisioning has completed.

Survey automation is a communication-stage consequence. The
`SurveyTriggerHandler` consumes committed ticket-resolution and field-completion
events and delegates eligibility and idempotent invitation creation to
`communications.surveys`. It never polls ticket or work-order state. Only
Surveys whose lifecycle status is `active`, whose compatibility flag remains
enabled, and which have valid questions can create invitations.

## Retry contract

`EventStore.failed_handlers` is the current failure manifest. Historical
`EventHandlerAttempt` rows remain an audit trail and are only a compatibility
fallback for old records without a manifest. A retry:

1. does not rerun handlers that already succeeded;
2. treats those successes as satisfied dependencies;
3. retries failed predecessors before their blocked dependants;
4. records dependency suppression as `blocked`, not as a handler failure;
5. replaces the failure manifest with only the current attempt's failures.

## Change rules

Every production handler must be declared once in `HANDLER_CONTROLS`, own a
unique capability, and expose a valid event scope. Every explicit dependency
must target a handler subscribed to the same chained event. Startup and CI fail
on duplicate handlers, missing scopes, unknown event types, missing dependency
targets, dependency cycles, or a chain with fewer than two handlers.
