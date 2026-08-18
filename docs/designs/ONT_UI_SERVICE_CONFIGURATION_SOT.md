# ONT UI service configuration source of truth

Status: implementation in progress

## Decision

`network.ont_service_configuration`
(`app.services.network.ont_service_configuration`) is the application
coordinator for customer-service configuration submitted from the ONT Configure
UI. The supported path is:

```text
typed UI command
  -> assignment-scoped configuration head and immutable revision
  -> desired configuration + exact-service WAN intent + dialer projection
  -> tracked network operation + transactional dispatch
  -> ONT reconciler delivery
  -> current-revision readback
  -> lifecycle-scoped UI projection
```

Saving desired state is not delivery evidence. An operation becomes verified
only when the reconciler reads the device after application and finds no
current-revision drift.

## Ownership

The coordinator owns:

- admission of an assigned ONT configuration request;
- configuration head, revision, fingerprint, and operation identity;
- atomic coordination of desired state, WAN intent, credential projection,
  operation, and dispatch;
- replay, same-revision repair, and superseding-revision policy;
- the typed current-configuration projection and next action consumed by UI.

It does not own:

- assignment identity: `network.ont_assignment_identity`;
- WAN declarations: `network.ont_wan_service_intent`;
- PPP credential truth: `access.radius_projection`;
- CPE credential projection: `network.cpe_dialer_credential`;
- delivery authorization: `network.ppp_delivery_authorization`;
- device convergence or `sync_status`: `network.ont_reconcile_projection`;
- GenieACS/OLT transport;
- operation lifecycle or transport: `network.operation_ledger` and
  `network.operation_dispatch`.

Adapters create and close sessions. Configure, retry, execution, and reviewed
repair commands enter `execute_owner_command` once on a transaction-free
session. Participant owners remain flush-only.

## Lifecycle identity

`OntServiceConfigurationHead` is unique for one exact `OntAssignment`. Reusing
an ONT for another assignment creates another head; timestamps are never used
to infer whether evidence belongs to the current lifecycle.

`OntServiceConfigurationRevision` is immutable command evidence at
`(head_id, revision)`. It records a keyed fingerprint, idempotency key, typed
section, desired-change evidence with secrets redacted, operation identity,
delivery phase, waiting/failure reason, and verification time.

For WiFi revisions, the executor derives a typed field-only delivery scope
from this redacted evidence. The actual SSID and encrypted password remain in
canonical desired state. Initial delivery may force those explicitly admitted
fields even when ACS has no readable observation; readback-only attempts retain
the scope but never force or repeat a write-only password action.

The current reconciler projection is bound explicitly on `OntUnit` to:

- configuration head ID;
- assignment ID;
- desired revision;
- network operation ID.

`OntProvisioningEvent` carries the same nullable bindings. Legacy unbound rows
remain append-only historical evidence and never become current merely because
their timestamp is recent.

New configuration operations must have all four identifiers. Migration does
not guess bindings for existing errors or events.

## Admission, locking, and atomicity

Lock order is `OntUnit -> active OntAssignment -> configuration head -> active
WAN intent -> credential inputs -> operation/dispatch`. Admission rechecks the
authenticated scope, exact active assignment and subscription, PON identity,
authorization/commissioning readiness, submitted section, and effective config
pack before mutation.

A command advances the head once and atomically:

1. declares, activates, preserves, or replaces the exact-service WAN intent;
2. projects PPPoE dialer values only from the active subscription credential;
3. persists the typed desired changes;
4. creates the revision and tracked `ont_service_config` operation;
5. stages `ont_service_config_apply.v1` in the durable dispatch outbox.

Any failure rolls back every item above. The route never commits, calls an OLT
or ACS adapter, invokes `reconcile_ont`, or publishes a Celery task.

## Idempotency and supersession

The idempotency scope is one assignment head. Reusing a key with the same keyed
material-input fingerprint replays the original operation; reusing it with
different material input is a typed conflict. A changed command creates
revision N+1 even when revision N failed. A stale worker checks assignment,
head, revision, and operation under lock before device contact and cancels
itself when any identity was superseded.

An ordinary re-submission cannot retry the same failed revision. The explicit
`RetryOntServiceConfigurationCommand` is the only same-revision repair path and
uses the reconciler's repair mode. Failed attempts remain in the operation
ledger and provisioning event history.

## Delivery and readback

The durable worker claims an existing dispatch; it never creates an operation
after broker delivery. It records these distinct phases:

- `saved`: atomic desired state exists;
- `queued`: durable dispatch awaits/has reached the broker;
- `applying`: the exact revision is being reconciled;
- `readback_pending`: a write landed but fresh device evidence is not yet
  sufficient;
- `delivered_unverified`: ACS accepted the exact LAN/DHCP block, but deployed
  firmware does not expose its mask and pool fields for exact readback;
- `verified`: current-revision readback has no actionable drift;
- `failed`: the exact revision/operation failed;
- `superseded` or `retired`: no longer current.

Readback-pending work remains on the same operation and uses bounded delayed
verification dispatches. It is never rendered as configured.

## Return to inventory

`network.ont_reconcile_projection` exposes the typed flush-only participant
`retire_ont_reconcile_projection_for_inventory`. The canonical return flow calls
it only after external cleanup succeeds. It retires the assignment head,
invalidates lifecycle bindings, clears current reconciler status/error through
the reconciler owner, and removes the current observation. WAN intents are
retired separately by their existing owner. Events, revisions, operations, and
retired intent rows remain history.

A stopped or partially failed inventory return does not call the participant
and therefore preserves the fault. Repeating a successful return is a no-op.

## UI projection and action contract

The ONT Configure page is an editor for one asynchronous transition.

- Audience: staff with `network:ont:write` configuring one assigned,
  commissioned ONT.
- Authority: `network.ont_service_configuration` supplies current lifecycle,
  revision, phase, exact operation, waiting/failure reason, last verified
  observation, and one valid next action.
- Submission: one typed section per request; success immediately shows
  “Configuration queued” and the operation ID.
- VLAN: show the effective customer VLAN and typed source (`config_pack`,
  `service_intent`, or `reviewed_override`).
- PPP: show only a masked derived username/provenance. The form neither accepts
  nor exposes the PPP password or allows an operator-authored username.
- LAN IP block: choices come only from active `ip_address` Catalog offers via
  `service_intent.ip_block_catalog`; the command owner converts the typed prefix
  to a device mask and refuses a prefix no longer present in the active Catalog.
  Sizes without an active subscriber entitlement remain visible but disabled;
  the subscription lifecycle must grant the offer before configuration.
  A `/32` disables and forbids DHCP. Mask-only edits force the complete
  write-only LAN/DHCP parameter block to ACS rather than relying on the readable
  DHCP-enabled flag.
- History: current summary reads only events bound to the active head and
  revision. Legacy, retired, and superseded events appear only in a separate
  evidence/history section.
- Action: `retry_current_configuration` is shown only when the owner returns it;
  templates do not infer eligibility from status strings or old failures.
- Responsive behavior: desktop and mobile preserve assignment identity,
  revision, operation, phase, reason, and next action. HTMX refreshes the owner
  projection without optimistic success.

## Legacy drift repair

The operator command is dry-run by default and reports unbound legacy errors,
inventory-returned ONTs with later failures, new assignments carrying an old
projection, active assignments without a head, active PPP intent without a
tracked configuration operation, and retired intent presented as current.

Execution requires exact ONT IDs, actor, reason, reviewed evidence, and an
idempotency key. It invokes the coordinator's reviewed repair command and the
reconciler participant; it never performs bulk SQL or adopts legacy evidence by
timestamp. Production repair of any named ONT remains a separate authorized
operator action.

## Migration and rollback

The schema change is additive. PostgreSQL enum values are added before use;
heads, revisions, and nullable evidence bindings are then created. No legacy
row is updated or deleted. Downgrade removes only the additive schema and is
therefore unsuitable after new configuration evidence has been accepted;
production rollback is forward-fix once the owner is enabled.

Lock and statement budgets follow the existing network-operation and
reconciler limits. Fresh-baseline migration tests and predecessor-to-head
rehearsal are both required before promotion.
