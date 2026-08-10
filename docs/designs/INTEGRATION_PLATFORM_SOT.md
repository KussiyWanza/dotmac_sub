# Integration Platform Source-of-Truth Design

Status: implemented source-of-truth architecture, 2026-07-20. The first-party
CRM, ERP, direct Meta WhatsApp, Meta social inbox, Paystack, Flutterwave, and HTTP webhook paths
have completed cutover. Their former direct transports and duplicate delivery
stores are retired by migration `380_integration_platform_cutover`.

## Decision

Dotmac Sub will provide a capability-based integration platform for external
systems. Product surfaces may call the extensions plugins; the architecture
calls them connectors because they translate bounded, typed contracts between
Sub and an external system.

The platform is not a general-purpose in-process plugin loader. A connector
must not mount arbitrary application routes, import Sub ORM models, write Sub
domain tables, select business policy, or become the only copy of operational
truth. Sub's existing domain owners continue to decide and persist payments,
subscriber state, access, tickets, work orders, network intent, communications,
and official timeline events.

The integration control plane owns connector definition, installation,
capability binding, transport execution evidence, and repair. It delegates
cadence to `scheduler.registry`, domain-event persistence to `events.store`,
and secret reference resolution to `secrets.reference_store`.

## Implemented baseline

The integration surface now has one executable connector contract:

- `app.services.integrations.registry` owns deterministic manifests for every
  platform-managed connector and its versioned capabilities.
- `integration.installations` owns immutable configuration revisions, secret
  references, installation lifecycle, and enabled capability bindings.
- `integration.runtime` is the only execution path for CRM, ERP, direct Meta
  WhatsApp, Facebook Messenger, Instagram Login, Paystack, and Flutterwave
  transport operations.
- `integration.delivery` is the only outbound HTTP webhook subscription,
  delivery, retry, dead-letter, and replay owner.
- `integration.inbox` is the only durable receipt and replay-evidence owner for
  CRM, WhatsApp, and payment provider webhooks.
- `integration.jobs` requires a versioned capability binding; string
  adapter/action dispatch is not accepted.
- CRM, ERP, payment, support, communications, and operations domain services
  remain the only writers of their business state. Connectors return typed
  observations or receipts and never write those domain tables.

The removed direct provider services, integration hooks, webhook endpoint
tables, CRM webhook delivery table, and payment dead-letter table are migration
history, not compatibility paths.

## Goals

1. Add a connector without adding provider branches to generic orchestration.
2. Give every connector operation a typed, versioned domain contract.
3. Support multiple configured installations of one connector definition.
4. Make inbound, outbound, scheduled, and interactive operations durable,
   observable, replay-safe, and repairable.
5. Isolate independently released connector code from Sub's web process,
   database, cache, host filesystem, and unrestricted network access.
6. Keep credentials behind the canonical secret and encrypted-field owners.
7. Upgrade integrations one coherent, reviewable vertical slice at a time
   without duplicate external sends or domain writes.

## Non-goals

- Runtime `pip install`, arbitrary package upload, or arbitrary code download
  from the admin UI.
- A generic `execute(action, payload)` escape hatch into Sub domains.
- A generic field-mapping engine that decides business state.
- Dynamic connector-owned FastAPI routes or templates.
- Exactly-once transport. The platform provides at-least-once delivery with
  stable idempotency and reconciliation.
- Moving payment, accounting, communications, subscriber, network, support,
  or work-order authority into the integration control plane.

## Concepts

### Connector definition

An immutable, deployed manifest plus an implementation. Its stable key,
connector version, contract API version, capability declarations,
configuration schema, secret bindings, data classifications, egress policy,
inbound endpoints, runtime type, and health operation are version-controlled.

The deployed code or signed artifact is authoritative. A database registry
snapshot may preserve discovery and approval evidence, but operators cannot
edit a definition into capabilities its deployed implementation does not have.

### Installation

A configured local instance of one connector definition. One definition may
have several installations, such as sandbox and production payment accounts or
separate WhatsApp Business accounts. An installation references an exact
definition version, artifact digest, and immutable configuration revision.

### Capability binding

An explicit grant allowing an installation to implement one versioned domain
port within an approved scope. Example identifiers include:

- `events.sink.v1`
- `webhooks.ingress.v1`
- `sync.source.v1`
- `crm.ticket_observation.v1`
- `messaging.send.v1`
- `messaging.receive.v1`
- `payments.intent.v1`
- `payments.reconcile.v1`
- `accounting.export.v1`

The owning domain defines each port's input, output, validation, idempotency,
and decision semantics. The integration registry records compatible
implementations and bindings; it does not redefine those semantics.

### Operation

One execution request pinned to an installation, capability, connector
revision, configuration revision, deadline, trigger, actor, correlation ID,
and idempotency key. Its durable evidence contains sanitized input and output
summaries, attempt classification, and external receipt references, never
secret values.

### Observation and command

An inbound connector produces a typed observation. A domain resolver evaluates
that observation and may submit a command to the canonical domain owner. The
connector never converts external state directly into a Sub table write.

## Live ownership

| Owner | Responsibility |
| --- | --- |
| `integration.registry` | Deployed definitions, manifest validation, compatibility, capability implementation catalogue |
| `integration.installations` | Installation lifecycle, immutable config revisions, connection-validated capability grants and bindings |
| `integration.runtime` | Runner selection, version pinning, operation envelopes, deadlines and cancellation |
| `integration.inbox` | Verified inbound receipt, provider-event dedupe and processing lifecycle |
| `integration.delivery` | External delivery rows, queueing, retry, dead letter and replay |
| `integration.jobs` | Integration targets, capability-bound jobs, pinned runs and their operator lifecycle |
| `integration.sync` | Sync orchestration, leases, checkpoints and per-record outcomes |
| `events.store` | Transactional domain-event persistence and handler-attempt evidence |
| `scheduler.registry` | Effective cadence, enablement and Celery schedule registration |
| `secrets.reference_store` | OpenBao reference parsing, resolution and bounded caching |
| Each domain owner | Business projection, eligibility, decision, command and canonical write |

Service names enter the executable SOT registry only when their modules and
real application/operator callers exist. Future identity-link or aggregate
health modules do not become owners merely because the concepts appear here.

## Connector manifest contract

A manifest is deterministic and contains no secrets. Conceptually:

```yaml
api_version: dotmac.io/integrations/v1
key: dotmac.crm
version: 2.1.0
runtime:
  type: builtin-worker
capabilities:
  - id: crm.ticket_observation.v1
    modes: [scheduled, manual]
config:
  schema: crm-config-v1
secrets:
  - name: service_credentials
    required: true
data_access:
  reads: [subscriber.external_identity]
  emits: [crm.ticket_observation.v1]
egress:
  hosts: [crm.dotmac.io]
health:
  operation: connection.validate.v1
```

Registry validation rejects duplicate keys, incompatible API versions,
unknown capabilities, invalid schemas, undeclared secret bindings, and runtime
requirements outside the approved policy.

## Runtime and trust tiers

### Built-in connectors

First-party connectors may ship in the Sub repository, but execute in
integration worker processes rather than FastAPI web processes. They implement
the same operation envelope and domain ports as an isolated connector. Routes,
tasks, and webhooks remain thin adapters to the control plane.

### Independently released connectors

External connectors execute out of process as approved, signed, digest-pinned
OCI workloads. They receive no Sub database or Redis credentials, no host
filesystem mounts, no OpenBao master token, and no unrestricted network. They
run non-root with a read-only filesystem, bounded CPU/memory/time, declared
egress, and authenticated least-privilege Sub APIs.

An external runner receives a short-lived credential lease or in-memory secret
material for the exact installation binding. Secret values never enter the
database operation payload, Celery arguments, logs, traces, audit rows, or
runner artifacts.

The runtime contract exposes definition/capability introspection, static and
connection validation, execute, cancellation, and health. Every request binds
the operation ID, capability version, deadline, config revision, credential
binding, sanitized payload, and correlation context.

## Persistence model

The platform schema consists of:

- `integration_installations`
- `integration_config_revisions`
- `integration_capability_bindings`
- `integration_event_subscriptions`
- `integration_inbox`
- `integration_deliveries`
- `integration_checkpoints`
- extended `integration_jobs`
- extended `integration_runs`
- extended `integration_records`

Definitions remain code/artifact-owned. A registry snapshot table is optional
and, if added, is an audit projection rather than an install authority.

Required constraints include:

- unique provider event identity per installation and ingress capability;
- unique outbound delivery per source event and capability binding;
- immutable, content-hashed configuration revisions;
- exact connector and configuration revisions on every run;
- checkpoint advancement only after the corresponding page commits;
- bounded payload and response retention by data class;
- no secret-bearing columns outside the declared secret/credential inventory.

## Execution flows

### Pre-activation webhook verification

Provider callback verification is an installation-owned, read-only setup
query, not an inbound message operation. It may compare the provider-presented
verification token against the exact secret reference on the single statically
valid installation while that installation is `disabled` or `enabled`. The
query returns only a sanitized acceptance result; secret material is never
returned to the HTTP adapter, persisted, or logged.

This setup exception does not grant an ingress capability. Webhook `POST`
requests, signature materialization, receipt creation, and message consequences
continue to require an enabled receive binding. Draft, validating,
quarantined, retired, ambiguous, invalid, or secret-unavailable installations
fail closed during callback verification.

### Outbound domain event

1. The domain owner commits its state change and stages an `EventStore` row in
   the same transaction.
2. The integration event handler creates one deduplicated delivery for each
   matching enabled binding using an owner-produced data projection.
3. A worker claims the delivery with a lease and sends it using a stable
   provider idempotency key.
4. The delivery records the external receipt and terminal or retryable result.
5. Transient failures back off; permanent failures and exhausted retries enter
   a dead-letter state that requires an authorized replay.

### Inbound webhook

1. A generic installation endpoint resolves an enabled ingress binding.
2. The gateway enforces method, content type, body size, rate, and timeout.
3. Provider-specific signature verification runs synchronously.
4. A verified provider event is inserted with a unique identity and payload
   digest. Duplicate receipt returns the existing state.
5. Asynchronous processing normalizes the receipt into a typed observation.
6. The relevant domain resolver and command owner decide and persist the
   consequence idempotently.

Receipt identity is never proof that the consequence completed. A receipt with
an incomplete consequence resumes processing rather than becoming a no-op.

### Scheduled or manual sync

1. `scheduler.registry` or an authorized operator enqueues a job ID.
2. `integration.sync` claims a lease for the installation/capability pair.
3. The run pins definition, artifact, capability, and config revisions.
4. The connector fetches one bounded page from the current checkpoint.
5. Each row becomes a typed observation and receives a per-record outcome from
   the domain owner.
6. The checkpoint advances only after the whole page transaction commits.
7. Partial success, retryable rows, conflicts, and terminal rejection remain
   distinct and observable.

### Interactive provider command

The domain owner validates eligibility and selects an explicitly configured
binding, then invokes its typed port with a deadline and idempotency key. It
owns persistence of the returned provider receipt. A timeout with an ambiguous
remote outcome becomes `reconciliation_required`; it is not blindly repeated.

### OAuth connection

OAuth uses one-time state, PKCE where supported, exact redirect allowlists,
manifest-versus-granted scope validation, canonical token storage, scheduled
refresh, expiry health, and explicit revoke/disconnect. An OAuth grant belongs
to an installation; it is not a parallel connector registry.

## Installation lifecycle

The lifecycle is `draft -> validating -> disabled -> enabled`. Security or
integrity policy may move any non-retired installation to `quarantined`.
Uninstall is a soft `retired` state that preserves runs, deliveries, receipts,
links, approvals, and audit evidence.

Health is a separate projection: `unknown`, `healthy`, `degraded`, or
`unavailable`. Enabled does not imply healthy.

Enablement requires current manifest/config compatibility, resolvable secret
references, successful connection validation, explicit capability grants,
required domain policy bindings, and current security approval.

Adding a capability to an enabled installation uses
`provision_installation_capability`. The owner locks the reviewed installation,
checks its exact manifest pin and prior binding state, stages the new grant,
performs static and live connection validation, and commits the restored
enabled installation atomically. Failed validation rolls the entire change
back, including the temporary disabled state of sibling bindings.

## Security and authorization

Every manifest declares requested capabilities, data classes, secret bindings,
external hosts, inbound endpoints, and resource requirements. Operators grant
only the required subset.

RBAC must separate catalogue read, installation read/write, credential bind,
capability approval, run execution/cancellation, dead-letter replay, and
external artifact approval. Money-moving, network-changing, and high-PII
capability grants require approval independent of routine configuration edits.

The platform enforces owner-produced field projections, data minimization,
payload size and retention limits, log/trace redaction, SSRF and egress policy,
rate limits, concurrency leases, circuit breakers, kill switch, quarantine,
and complete config/grant audit. Error responses expose classifications and
correlation IDs, not provider secret material or raw sensitive responses.

An embedded external UI is not a code plugin. The safe default is a link to an
allowlisted external application. Any iframe capability requires explicit CSP,
frame-source, cookie, origin, and permission review and cannot inherit Sub
credentials.

## Reliability and reconciliation

External delivery is at least once. Stable idempotency keys, transactional
inbox/outbox rows, unique constraints, bounded retries, and reconcilers make it
safe and repairable.

Retries classify rate limits, transient transport failure, provider 5xx,
permanent validation/auth failure, conflict, and ambiguous outcome separately.
Retry policy comes from the capability contract within platform bounds; a
connector cannot request unbounded retries.

Backlog, next attempt, lease expiry, circuit state, and dead-letter reason are
durable. Reconciliation reads authoritative Sub intent and external facts,
repairs the external projection or resumes an incomplete local consequence,
and never creates a competing business-state decision path.

## Observability

The same correlation ID follows the domain command/event, queue task, operation,
runner call, external request, receipt, and reconciliation result. Per
installation and capability, the platform exposes success/failure rate,
latency, queue age, backlog, last success, authentication expiry, rate limits,
circuit state, retries, dead letters, connector revision, and config revision.

Health is derived from these facts. Merely being installed or enabled never
produces a healthy state. Logs, metrics, traces, activity projections, and
audit records contain identifiers and classifications but no secrets.

## Versioning and upgrades

Definitions use semantic connector versions, a platform API major version, and
independently versioned capabilities. Installations pin an exact artifact
digest. A breaking domain contract receives a new capability major version.

Manifest contents are immutable for one connector version. The reviewed
manifest-pin ledger at
`tests/architecture/connector_manifest_pins.json` changes additively: changing
configuration schema, secret declarations, runtime, capabilities, data access,
egress, or health metadata requires a new semantic connector version. The two
Paystack `1.0.0` digests are a documented, shrink-only historical anomaly from
the payment control-plane cutover. Meta Social also retains both deployed
`1.0.0` digests after `auth_mode` was added in place; the original production
pin and the later individual-auth pin remain executable until explicit
adoption closes their rollback windows. No new version/digest collision is
allowed.

The ledger records the digests this code produces, not the digests any
environment has installed, so it cannot by itself detect that a rebuilt
historical manifest has drifted away from a live pin. A historical manifest is
reconstructed by its builder rather than stored, and editing that builder
silently changes every version it rebuilds. Twice now the ledger was updated to
match a drifted rebuild and stayed green while a production pin went
unrepresented, which the deployment gate then caught as `unavailable` — after
the release had already started. When a builder gains a field, gate it behind
a per-version flag so prior versions rebuild byte-identically, and treat a
changed historical digest as a break rather than a ledger update.

Upgrade flow is expand, adopt, then contract:

1. Deploy the new version while retaining every still-pinned prior definition
   and compatible runner for a bounded adoption/rollback window.
2. The deployment gate executes
   `scripts.integrations.verify_manifest_pins` from the candidate image and
   refuses service replacement if an enabled installation pin is unavailable.
   A supported historical pin is executable but reported as adoption debt.
3. Preview adoption through the `integration.installations` owner. The preview
   names the installed pin, exact deployed target pin, lifecycle state, and
   configuration/secret-reference/capability compatibility errors.
4. Submit the reviewed exact expected and target pins through
   `adopt_installation_manifest`. The owner locks the installation, rejects
   stale review evidence, updates version and digest atomically without
   changing lifecycle or capability state, and records a non-secret
   `integration.installation.manifest_adopted` event.
5. Observe runtime and customer-flow evidence. A reviewed rollback adopts the
   retained prior exact pin through the same owner.
6. Remove a historical definition only in a later release after the deployment
   report proves no enabled installation still pins it and the rollback window
   is closed.

Configuration migration, connection validation, contract tests, shadowing, and
canary execution remain required when the target changes those contracts. A
deployment never silently re-pins installations: that would erase the approval
boundary the digest provides. The operator procedure is
`docs/runbooks/CONNECTOR_MANIFEST_ADOPTION.md`.

## Admin and API surfaces

The marketplace shows approved deployed definitions, not downloadable code.
Installation forms render from safe configuration metadata, with secret fields
represented only by reference/binding state. Domain-specific pages call their
canonical owner and use the same capability binding as API/mobile callers.

Dynamic connector-owned application routes are prohibited. Inbound traffic
uses generic platform routes keyed by installation and endpoint; outbound and
interactive traffic uses typed service ports.

## Authority cutover record

| Concern | Retired owner/path | Live owner/path | Result |
| --- | --- | --- | --- |
| Connector catalogue | File discovery and static catalogue projections | Manifest-based `integration.registry` | Runtime registration and manifest validation are required |
| Installation configuration | Provider environment settings and provider-specific credential columns | `integration.installations` with immutable config revisions and secret references | Platform-managed callers resolve enabled version-pinned bindings only |
| Sync dispatch | String `adapter_key/action` selection | Capability-bound `integration.sync` through `integration.runtime` | Active jobs require a capability binding |
| CRM | Direct `CRMClient` construction and CRM-specific delivery records | `dotmac.crm` capabilities plus `integration.inbox` | All subscriber, ticket, operational, portal, quote, and inbound-event calls use the runtime; enabled ticket pull additionally requires one connection-validated ticket binding and one active bound manual job |
| Outbound webhooks and hooks | `events.webhook_deliveries`, webhook endpoint tables, and `integration.hooks` | `integration.delivery` using `events.deliver.v1` | Duplicate tables, services, routes, tasks, and CLI execution are removed |
| WhatsApp messaging | Settings-backed provider client | Direct Meta `messaging.send.v1`, `messaging.receive.v1`, and `messaging.templates.read.v1` bindings | Outbound callers and the verified inbound route use one installation |
| Nextcloud Talk staff notifications | Direct credentials and legacy `/room/{token}/message` calls | `nextcloud.talk` `collaboration.message.send.v1` capability plus communications-owned staff mapping, room cache, and notification outbox | Assignments and explicit mentions stage locally; the worker creates/reuses a one-to-one room and posts with a deterministic reference ID |
| Meta social inbox | Settings-backed app secrets, expired OAuth projections, and direct `meta_pages` delivery | `meta.social` with separate Facebook Page and Instagram Login credentials behind `messaging.send.v1` and `messaging.receive.v1` | New delivery and webhook verification use one version-pinned installation; production traffic cutover remains operator-gated |
| ERP | Direct ERP client construction | `dotmac.erp` versioned capabilities | Outbox, inventory, operations, expense, purchasing, and regulatory calls use the runtime |
| Payments | Direct Paystack and Flutterwave services plus a payment-specific webhook dead-letter store | Billing-owned decisions using typed payment capabilities and `integration.inbox` | Intent, signature verification, reconciliation, refund, and replay evidence use one binding |

Migration `380_integration_platform_cutover` removes the retired tables,
columns, settings, and enums. Its downgrade is intentionally unavailable: the
old paths are not a rollback mechanism. Rollback means correcting or disabling
the current installation/binding while authoritative Sub state and durable
delivery/inbox evidence remain intact.

## Implemented slices

1. Manifest registry, immutable installation configuration, capability
   bindings, DB-free runtime runners, and architecture guards.
2. Capability-bound jobs and sync execution with pinned run evidence.
3. Canonical outbound HTTP delivery and installation-bound inbound receipts.
4. Direct Meta WhatsApp send, receive, templates, verification, and replay.
5. DotMac ERP outbox, status, inventory, operational context, and regulatory
   capabilities.
6. DotMac CRM subscriber/ticket/operations observations, portal sessions,
   quote commands, and verified inbound events.
7. Paystack and Flutterwave payment intent, webhook verification,
   reconciliation, and refunds while billing retains financial authority.
8. Destructive cutover migration and removal of superseded application paths.
9. Explicit CRM ticket-observation provisioning, exact job activation, and a
   deployment/scheduler readiness invariant preventing an enabled control from
   running without its binding and job.
10. Meta social inbox transport with distinct Facebook Page and Instagram
    Login account bindings, Meta-owned webhook verification, and no WhatsApp or
    expired-OAuth credential fallback.
11. Nextcloud Talk staff notification transport with a pinned installation,
    OpenBao app-password reference, public-HTTPS egress validation, explicit
    `SystemUser` mappings to exact immutable Nextcloud user IDs, cached one-to-one
    rooms, deterministic chat references, and asynchronous retry/reconciliation.
    Ordinary internal spaces are preserved because Nextcloud permits them in user
    IDs; email addresses and display names are not substituted for the provider
    identity. The capability binding's `approved_egress_hosts` must explicitly
    include the hostname from the installation URL before static validation can
    enable it.

Signed external artifacts and OAuth installation grants require separate
approved designs before they can become live owners. They are not implicit
capabilities of the current built-in runtime.

## Per-slice cutover gates

Every behavior slice must prove:

- named old and new owner, shadow phase, cutover, rollback, and fallback
  retirement;
- no duplicate external send and no duplicate Sub domain write;
- manifest, contract, config, and capability validation;
- crash/restart behavior at every durable state boundary;
- stable idempotency and safe replay of incomplete work;
- shadow parity on a representative cohort;
- secret, payload, log, trace, and error redaction;
- granular RBAC and high-risk capability approval;
- bounded retry, dead-letter, cancellation, and reconciliation behavior;
- health, metrics, tracing, audit, and operator repair actions;
- architecture tests that prevent connector ORM/domain writes;
- updated source-of-truth documentation and executable registry;
- rehearsed rollback to the pinned prior implementation/configuration.

## Permanent invariants

1. A connector is a transport and translation implementation, never a business
   decision system.
2. Domain owners produce outbound projections and decide inbound consequences.
3. Connector code cannot write Sub domain tables.
4. Secrets remain behind the canonical secret and credential owners.
5. Every external side effect has durable intent, idempotency, attempt evidence,
   and reconciliation.
6. Installation state, health, and business state are distinct.
7. Definitions are deployed and approved; the admin UI does not install
   arbitrary executable code.
8. No migration creates two active writers or two active senders for one flow.
