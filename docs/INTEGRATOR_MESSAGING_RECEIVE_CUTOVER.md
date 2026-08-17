# Adopting the Integrator for `messaging.receive.v1` — shadow, cutover, rollback

**Status: shadow path built, cutover NOT executed and NOT authorized.**

Nothing in this repository repoints a provider callback. No host has been
touched. This document says what must be true before one is repointed, what
gets retired in the same change, and how to go back. Repointing a production
callback requires Michael to name the target explicitly; this document does not
substitute for that.

Companion specification: `dotmac_starter_mt/docs/superpowers/specs/2026-08-15-sub-messaging-receive-port.md`.
Standards: ADR-0024 (applications compose by synchronizing data), ADR-0018 (an
exemption states an enforceable premise).

---

## 1. What exists now

| Piece | Where | What it does |
|---|---|---|
| The port | `app/api/integrator_observations.py` | `POST /api/v1/integration/observations/{capability_binding_id}` — authenticates the Integrator, records, delegates, answers |
| The shadow port | same file, `.../mirror` | Same envelope, **writes nothing**, returns a parity verdict |
| Envelope normalization | `app/services/team_inbox_integrator_envelope.py` | Pure envelope → `RecordProviderObservationCommand` |
| The mirror harness | `app/services/team_inbox_integrator_mirror.py` | Field-by-field comparison and cutover verdict |
| Operator CLI | `scripts/migration/integrator_observation_mirror.py` | Batch parity over a captured envelope file, exits non-zero while blocked |
| Transport identity | `app/services/integrations/connectors/integrator_http.py` + registry key `dotmac.integrator.http` | Gives the Integrator its own installation, binding, receipts and quarantine state |
| Scopes | `integration:observations:write`, `integration:observations:mirror` | Seeded by `scripts/seed/seed_rbac.py` and migration `536_integrator_ingress_scopes` |

Both producers can run at once. That is the point of the shadow.

---

## 2. Three decisions worth re-reading before touching anything

### 2.1 Sub authenticates the Integrator, not the provider

Sub's existing inbound routes verify a provider HMAC over raw bytes. Correct
when Sub talks to the provider. Wrong here: the Integrator talks to the
provider, verifies the signature over the bytes it actually covers, and then
re-serializes into a provider-neutral envelope. Re-checking a provider
signature at Sub's door would be checking a signature over a body that is not
the signed body.

So the port requires a scoped `ApiKey` machine principal. `ApiKey.scopes` is
already fail-closed on empty and `revoked_at` already gives revocation — no
fourth credential shape was invented.

### 2.2 The provider field names the provider, never the transport

**This is the decision that makes the cutover safe, and it is a deliberate
deviation from § 5.3 of the specification, which proposed a new
Integrator-sourced `InboxProvider` member.**

A WhatsApp message that reached Sub through the Integrator is still a
`meta_cloud_api` observation. If the Integrator recorded under a distinct
`integrator` provider, then `uq_inbox_provider_observations_identity`
— `(provider, provider_account_scope, provider_event_id)` — would give the same
upstream event **two different identities** either side of a cutover. Every
message in flight during the overlap would be recorded twice, processed twice,
and shown to the agent twice. It would also let a transport masquerade as a
provider in the domain identity, which is the coupling ADR-0024 removes.

Which transport carried an event is provenance, and provenance lives on the
`integration_inbox` receipt, which already names the binding and therefore the
installation. No new enum member was added. **This needs Michael's ruling to be
final** — see § 8.

### 2.3 Envelope `scope` is provenance and can never select a destination

The Integrator's binding carries a `LocalScope` (`inbox:support`). Sub records
it on the transport receipt headers and its routing owner never reads it. Sub is
authoritative for its own structure; a transport's opinion about a Sub team is
an observation like any other. This is Sub's half of the invariant
`dotmac_integration.destination_binding` enforces on the other side.

---

## 2.4 The wire contract — this is the half the Integrator was waiting on

The Integrator's ingress surface and receipt worker are built, but its
`ProductPortClient` deliberately was **not** written there. Authoring a wire
contract two systems must agree on, inside the transport, is what ADR-0024
forbids: the destination application owns its own accepted port, and the
transport learns the shape rather than inventing it. So the contract below is
the missing half, and it is settled here, by the owning product.

**Authoritative definition:** `app/schemas/integrator_observation.py`. That
module — not this table, and not a copy in the Integrator — is the contract. It
is `extra="forbid"` throughout, so an unrecognized field is a 422 rather than
something silently dropped.

`POST /api/v1/integration/observations/{sub_capability_binding_id}`
Header `X-Api-Key: <key scoped integration:observations:write>`

| field | type | notes |
|---|---|---|
| `capability_id` | string | must equal `messaging.receive.v1`; mismatch is **404**, not 403 |
| `contract_version` | int | `1` today; anything else is **409**, never a best-effort parse |
| `provider` | string | provider FAMILY (`meta_cloud_api`), never `integrator` — see § 2.2 |
| `provider_account_scope` | string | account within the provider (e.g. the WhatsApp phone-number id) |
| `provider_event_id` | string | the provider's own immutable id, **unprefixed**; Sub applies its own `message:`/`receipt:` namespacing |
| `channel` | string | `whatsapp`, `facebook_messenger`, `instagram_dm`, `facebook_comment`, `instagram_comment`, `chat_widget`, `email` — validated against the provider family |
| `observed_at` | RFC-3339, **tz-aware** | naive is a 400 |
| `payload_fingerprint` | 64 hex | canonical-JSON SHA-256 (`sort_keys`, `separators=(",",":")`) over the `message` **or** `delivery_receipt` object; recomputed by Sub, mismatch is 400 |
| `scope` | `{kind, ref}` | the Integrator's binding scope — **provenance only**, recorded on the receipt |
| `message` \| `delivery_receipt` | object | exactly one; both or neither is a 400 |

Responses: **200** `{observation_id, outcome, processing_status, replayed}` ·
**401** bad/absent/revoked/unscoped credential · **404** unknown capability or
binding · **409** collision or undeployed contract version · **400** malformed
envelope · **422** the consequence owner rejected the observation.

The shadow route is the same body at `.../mirror` with a
`integration:observations:mirror` key, and returns a parity verdict instead.

Two things the client author should not have to discover by experiment:

* **`provider_event_id` is sent raw.** Sub prefixes it. A client that sends
  `message:wamid.X` produces `message:message:wamid.X` and a duplicate.
* **The fingerprint covers the observation sub-object only**, not the whole
  envelope — otherwise it could not be computed before the envelope is
  assembled.

---

## 3. The shadow window — what to run, and what it proves

1. Create an `IntegrationInstallation` on connector `dotmac.integrator.http`
   with an enabled `messaging.receive.v1` binding. Record its binding id; the
   Integrator's destination profile needs it (see § 8.2).
2. Issue an `ApiKey` with `system_user_id` set and **only**
   `integration:observations:mirror` in `scopes`. A shadow credential that
   cannot write is what makes "shadow" a property rather than a promise.
3. Leave the provider callback exactly where it is. Sub's existing webhook stays
   the only producer.
4. Point the Integrator at `.../mirror`. It normalizes and Sub compares; nothing
   is recorded on either side of the comparison.
5. Collect verdicts. Or capture the Integrator's outbound envelopes to a file
   and run the CLI against a production-derived restore.

### The verdicts, and what each one means

| Verdict | Meaning | Blocking |
|---|---|---|
| `agrees` | Identity and every normalized field match | no |
| `field_disagreement` | Same observation, a normalized field differs | yes |
| `identity_shape_mismatch` | Sub recorded this same upstream event under a **different** identity | yes — the important one |
| `collision` | Same identity, different domain fingerprint. The producers disagree about what the provider said | yes — escalate, do not retry |
| `no_counterpart` | No matching Sub observation at all | yes |

`identity_shape_mismatch` is the finding this whole harness exists for. It is
exactly the condition that would double-record every message during the
overlap, and neither producer would look buggy in isolation.

`collision` never degrades into a silent duplicate. The observation owner
already raises `provider_event_identity_collision` for the identical case, the
live port answers 409, and the harness reports it as blocking rather than
folding it into a dedup.

---

## 4. The cutover gate — what must be true before a callback is repointed

Every one of these, with evidence, and none of them inferred:

1. **A non-empty population.** `compare_population` refuses to call an empty
   run safe. Absence of disagreement over zero events is not agreement.
2. **Every blocking reason count is zero**, on production-derived data, across
   at least one full traffic cycle including a weekend and a campaign send.
3. **Zero `identity_shape_mismatch`** specifically — call it out separately in
   the review even though it is covered by (2), because it is the one whose
   consequence is customer-visible duplication.
4. **Replay proven on both sides.** The same envelope delivered twice produces
   one observation and one consequence; the second call returns the first
   outcome.
5. **Collision proven to escalate**, not dedup, with the original
   `normalized_payload` byte-identical afterwards.
6. **The Integrator's write credential exists, is scoped to
   `integration:observations:write` only, and its `ApiKey` row is the only one
   holding that scope.** Verified by query, not by intent.
7. **Migration `536_integrator_ingress_scopes` has run on the target
   database.** The permission rows must exist before the port is load-bearing,
   or the surface is dark with green CI — the exact failure migration 477
   records.
8. **A named rollback owner and a rollback window**, agreed before the change,
   not discovered during it.
9. **Michael has explicitly named the production host and the callback being
   repointed.** No inference from an environment mapping.

---

## 5. The cutover, in the order it must happen

1. Re-scope the Integrator's `ApiKey` from mirror to write. (Issue a new key and
   revoke the old one rather than mutating scopes in place, so the change is a
   row an audit can point at.)
2. Point the Integrator at the write port. Both producers now write. This is the
   **producer-overlap window** — see § 6.
3. Watch. Duplicate conversations, duplicate messages, and
   `inbox_provider_observations` insert rate are the signals. § 2.2 is what
   makes this window boring; if it is not boring, stop and roll back.
4. Repoint the provider callback from Sub to the Integrator. Sub's webhook stops
   receiving. The overlap ends.
5. Only then, retire — see § 7.

Steps 1–3 are reversible without provider involvement. Step 4 is the one that
requires the provider console and a named host.

---

## 6. The producer-overlap window

Sub will briefly have Integrator and non-Integrator producers of one capability.
`messaging.receive.v1` is already bound in Sub by two Meta-family connectors,
and reusing the id is correct — it is provider-neutral and already means
"inbound message observation". Sequencing that retirement is **Sub's decision,
not the Integrator's**, and this document does not make it.

What the design already guarantees during the window:

- **One observation per upstream event**, because both producers compute the
  same identity (§ 2.2 and the `message:`/`receipt:` prefix in the normalizer).
  The second producer sees a `replayed` outcome, not a second fact.
- **Two transport receipts, one observation.** The receipt layer is keyed on
  `(capability_binding_id, provider_event_id)`, so each producer gets its own
  receipt on its own binding — which is what lets them be enabled, quarantined
  and retired independently.
- **Ordering is a non-issue.** Each observation is an immutable fact with its own
  identity. Nothing is buffered or reordered and the processing owner reaches the
  same end state regardless of arrival order.
- **Concurrency is a non-issue.** Two workers delivering one event produce
  exactly one observation; the loser sees the replay outcome, not an error. The
  observation owner already retries once past the unique-key race.

What is **not** guaranteed and must be watched: a message the Integrator
normalizes differently from Sub's webhook is a `field_disagreement` in shadow,
but during the overlap it becomes a **collision** and the second producer's
delivery is refused with a 409. That is the correct failure — content is
preserved and it escalates — but it means the shadow gate in § 4 must be clean
before entering the window, not during it.

---

## 7. What is retired, in the same change

A migrated boundary is incomplete until the old writers are gone. All of this
goes together, once the overlap has ended and the rollback window has closed:

1. **Sub's own receiver.** `app/api/inbox_webhooks.py` (`POST /webhooks/whatsapp/meta`
   and its `GET` verification challenge) and the WhatsApp half of
   `app/api/meta_inbox_webhooks.py`, plus their entries in `_CORE_ROUTER_SPECS`.
2. **Its credentials.** The WhatsApp app secret and verify token, and their
   OpenBao entries. A retired receiver holding a live provider secret is a
   standing liability with no compensating control.
3. **Its provider egress.** The `whatsapp` connector installation's binding for
   `messaging.receive.v1` — the send capability stays; only receive moves.
4. **Its ratchet debt.** Two defects in the current receiver that the new path
   deliberately does not inherit, and whose removal is the actual prize:
   - `app/api/inbox_webhooks.py` derives event identity from a **request
     digest** (`meta:{sha256(raw_body)}`). An approved fleet standard forbids
     that: identity comes from the provider's own event id scoped to the
     binding, and a derived identity must be labelled derived. A regrouped batch
     double-records under a digest key. The new port uses the provider's own
     event id.
   - `app/api/meta_inbox_webhooks.py::_whatsapp_signature_fallback_secret` has a
     bare `except Exception: return None` and borrows a secret across
     installations. It disappears with the receiver rather than being patched.
5. **The mirror**, last. Retire it only after the rollback window closes — it is
   the instrument, and removing the instrument while the patient is still on the
   table is how a regression becomes undetectable.

Removing (1)–(3) while leaving (4) would be the worst outcome: the defects would
survive in a path nothing exercises, invisible and unfixed.

---

## 8. Open — needs a ruling

1. **The `InboxProvider` deviation (§ 2.2).** The specification's § 5.3 proposed
   a new Integrator-sourced enum member. This slice deliberately does not add
   one, because it would double-record every in-flight message at cutover. The
   reasoning is above; the decision is Michael's. If the ruling goes the other
   way, the cutover needs a drain-and-freeze instead of an overlap, and § 5–6
   must be rewritten.
2. **Whose binding id is in the URL.** Sub's port is keyed on **Sub's**
   `IntegrationCapabilityBinding.id`, which is a different UUID from the
   Integrator's own `DestinationBinding.capability_binding_id` in a different
   database. Sub's binding id must be configured into the Integrator's
   destination profile. The mechanism for getting it there is not decided.
3. **How Sub publishes its capability declaration to the Integrator's
   assembly** — a checked-in manifest, a build-time artifact, or a
   `ModuleManifest` field. Left open by `provider-capability-sources.md` § 7.2;
   nothing here presumes an answer.
4. **The seven integration-platform tables Sub still owns**, which
   `dotmac-integration` was extracted from. Until Sub retires them, both sides
   hold a control plane for the same concept. Known duplication, not a
   contradiction — the port is built against Sub's existing services precisely
   so it survives that retirement without a rewrite.

---

## 9. Rollback

**Before step 4 (callback not yet repointed).** Revoke the Integrator's write
`ApiKey`. The port answers 401, the Integrator's deliveries fail, Sub's webhook
is still the producer and nothing was lost. No provider involvement, no deploy.

**After step 4 (callback repointed).** Point the provider callback back at Sub's
webhook. This requires the provider console and a named host, and it is why § 7
retirement happens *after* the rollback window, not during the cutover: the
receiver, its credentials and its binding must still exist to roll back to.

**If a collision is observed at any point.** Stop. Do not retry — a collision
means the two producers disagree about what the provider said, and retrying
delivers the same disagreement. Capture both `normalized_payload` values,
determine which producer is wrong, and fix the normalizer. The original content
is preserved by construction: the observation owner refuses the write rather
than overwriting.

**What rollback cannot undo.** Observations already recorded stay recorded —
they are immutable facts and that is correct. Rollback changes who produces
future observations; it never rewrites history.
