# Identity & Onboarding Owner Chain (rank 5 assessment)

**Status:** Assessed and partially delivered 2026-07-27 — existing
transitions conform; the invitation aggregate is now implemented
(`auth.access_invitations`); remaining nodes stay named feature slices
**System of record:** Sub
**Decision owner:** Michael

## Target chain

```text
invitation -> identity verified -> role/onboarding approved
  -> credential enrolled -> access activated
  (expiry/verification/approval deadlines as durable timers;
   security transitions fail closed)
```

## What already conforms

The front half of the chain was built to the owner-output protocol before
this program and needs no conversion:

- `auth.staff_provisioning` and `auth.reseller_onboarding`
  (COORDINATOR_MANAGED owner commands) emit typed provisioning outputs;
  `StaffInviteHandler` / `ResellerInviteHandler` / `PasswordRecoveryHandler`
  consume them fail-closed: each re-derives the recipient email digest and
  **raises** on drift (no swallow), and submits exactly one deduped
  communication intent. Capabilities are minted only at transport time by
  the owning domain (`issue_exact_reset_capability` /
  `materialize_*_email`), with typed `EphemeralActionRejected` codes.
- `auth.customer_credential_enrollment` (OWNER_MANAGED, full contract) is
  the reference security owner: rate-limited requests, transport-time
  minting, and a redeem path whose `_decode_token` enforces type, issuer,
  version, clock skew, expiry, **and a TTL upper bound that rejects
  over-long tokens even when correctly signed**. Single-use is enforced by
  credential existence; completion emits
  `customer_credential_enrollment.completed`, consumed by the
  cache-invalidation projection.
- `auth.token_signing` deliberately owns only signature and envelope —
  lifetime and claims stay with the calling domain. Timers must never move
  TTL authority into it.

## Deliberate non-gap

Credential enrollment does **not** activate the pending `subscriber`
PartyRole. Per the checked-in sales SOT
(`docs/designs/SALES_TO_SERVICE_LIFECYCLE_SOT.md`), only subscription
activation widens that role (`party.ensure_role` allows exactly
`pending → active` and fails closed otherwise). The identity chain's
"access activated" node for customers is subscription activation, already
chained in the sales slice.

## Missing nodes — require new aggregates (deferred feature slices)

These are feature builds, not chaining conversions; converting them without
their aggregates would invent state that has no owner:

1. **Invitation aggregate — DELIVERED.** `auth.access_invitations`
   (contracted owner, migration 435) records
   `issued → accepted / expired / revoked` for the staff, reseller, user,
   and subscriber invitation capabilities: every issuance path records the
   row and stages a durable `invitation_expiry_due` timer; reissue
   supersedes and cancels the prior timer; a completed reset stamps
   acceptance; the fired trigger drives the receipted expiry consumer.
   Rows are lifecycle evidence, never a grant — the capability's
   redeem-time TTL check remains the fail-closed gate.
2. **Verification/approval deadline models.** No phone-verification or
   onboarding-approval entity exists (`SubscriberNINVerification` and
   `PartyContactPoint.verification_status` are the nearest facts). The
   "role/onboarding approved" node has no backing state to time.
3. **Reseller commercial PartyRole.** `auth.reseller_onboarding` writes no
   `PartyRoleType.reseller`; the commercial-role lifecycle the chain
   invokes credential enrollment from does not exist yet.
4. **Uncontracted owners.** `party.registry`, `auth.token_signing`, and
   `sales.account_conversion` lack typed ServiceContracts;
   `sales.account_conversion` commits its own transaction and therefore
   cannot stage timers until it is wrapped in an owner command.

## Read-time TTL checks (recorded, acceptable until the aggregates exist)

Enrollment/conversion capabilities, `TicketAccessToken.expires_at`,
access-token expiry, and the audit-derived `invite_available_at` window are
redeem-time checks with no expiry transition. They fail closed at
redemption, so nothing grants access after expiry; what is missing is only
the affirmative expired-state evidence, which arrives with the invitation
aggregate.

## Related conversion delivered with this assessment

Ticket SLA clocks (`support.ticket_sla_clock`) had breach detection as
driverless dead code since the periodic scanner was retired. Each clock now
stages a durable `sla_breach_due` timer atomically with its creation
(inside the owning ticket command); the fired trigger drives the receipted
`consume_sla_breach_due` under `support.ticket_lifecycle`, which applies
`check_sla_breaches` — breach records, watchers, and the
`ticket.sla_breached` escalation — with a state-guarded no-op for paused,
completed, or already-breached clocks.

## CX acceptance deadline (delivered with this slice)

The sales chain's ready CX handoff now runs through the handoff owner's
receipted `consume_service_order_completion`, which stages a durable
`cx_acceptance_due` timer (`workflow.cx_acceptance_attention_hours`,
default 72h) when the handoff turns ready; the fired trigger drives the
receipted `consume_cx_acceptance_due`, flagging a still-ready handoff
`needs_attention` ("Customer acceptance overdue"). Acceptance itself
remains a staff decision.
