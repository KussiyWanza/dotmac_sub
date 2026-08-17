# Money-path operational closure

## Purpose and boundary

This runbook closes the operational findings left after the money-path code
invariants. It does not authorize a deployment or a production mutation. A
production or SSH step requires Michael to name the target host explicitly;
each mutation also needs its own approved change window and rollback owner.

Do not combine these findings into one bulk data edit. Their owners and lawful
evidence differ:

| Finding | Owner | Lawful completion evidence |
| --- | --- | --- |
| payment receipt email inactive | `communications.notification_service` | receipt-aware template test, activation audit, successful delivery |
| ERP tax-rate feed denied | authorization control plane plus ERP integration owner | least-privilege key cutover and successful `/api/v1/tax-rates/sync` checkpoint |
| prepaid funding quarantine | `financial.prepaid_funding_reconstruction` | signed complete-cohort artifact and materialization result |
| missing/stale prepaid billing anchor | `financial.prepaid_service_renewals` | exact active-entitlement target, reviewed fingerprint, repair audit |
| overdue receivables and collection cliff | `financial.dunning` | verified due-date basis, ranked owner work queue, per-case outcome |
| shared payment identifiers | customer identity owner plus Customer Operations | reviewed ownership/contact corrections and rebuilt identity index |
| dormant subscriptions still marked active | `access.subscription_lifecycle` | reviewed lifecycle evidence and owner-command outcome |
| 22 August invoice due-date cohort | `financial.invoices` plus Account Management evidence | proved basis or retained `unknown_unverified` quarantine |
| empty WHT source table | `financial.tax_accounting` | source-fact funnel proving either expected zero or a missing owner consequence |

## Release prerequisite

The receipt readiness signal, fixed recent-draft cohort, identity-log redaction,
and entitlement-backed NULL-anchor repair are code changes. Use them only after
the repository promotion sequence has deployed the exact accepted candidate.
Configuration work that uses existing production behavior is a separate change;
it must not be represented as proof that these code controls are deployed.

## 1. Payment receipt email

The `payment_received` email must contain both `{receipt_number}` and
`{receipt_url}`. A payment acknowledgement without those fields is not a usable
receipt. Activate email only; do not broaden the change to SMS or other channels.

1. Open `/admin/notifications/templates?status=inactive`, select the email
   template whose code is `payment_received`, and verify its subject/body against
   the checked-in seed in `app/services/settings_seed.py`.
2. Before activation, use the template's **Send test** action with a controlled
   Dotmac recipient. Supply a real staff-authorized receipt URL and a synthetic
   receipt reference in the preview variables. Never send a customer receipt to
   an unapproved address.
3. Confirm the test has no literal `{...}` tokens, the link is company-hosted,
   and the authorized recipient can open it. A test email proves transport
   acceptance, not final customer mailbox delivery.
4. Activate only that email template. Record actor, template ID, prior state,
   new state, and change reference; do not record customer addresses in the
   change artifact.
5. Acceptance is all of: `payment_receipt_email_template_ready == 1`, one new
   lawful payment event produces one receipt, queue delivery completes, and the
   authenticated receipt URL resolves for its intended customer.
6. Roll back by deactivating that same template if rendering, recipient scope,
   link authorization, or delivery fails. Payment settlement remains valid and
   must not be replayed merely to resend an email.

## 2. ERP AR tax-rate feed and API-key privilege

`GET /api/v1/tax-rates/sync` requires only `billing:tax:read`. The full bounded
AR feed uses these read scopes:

| Feed | Scope |
| --- | --- |
| invoices | `billing:invoice:read` |
| credit notes | `billing:credit_note:read` |
| payments | `billing:payment:read` |
| payment channels | `billing:channel:read` |
| billing accounts | `billing_account:read` |
| subscribers and resellers | `customer:read` |
| tax rates | `billing:tax:read` |

Do not merely add the missing tax scope to a key that also has `rbac:assign` or
`rbac:roles:read`. First confirm which bounded feed endpoints ERP actually
calls, then replace or rotate to the exact read-only scope set. Never print or
store the raw key in logs, tickets, the repository, or this runbook; use the
approved OpenBao pointer.

Cut over during a short dual-observation window: capture the old cursor, install
the replacement secret through the integration owner, fetch each required feed,
and compare returned cursors/counts without customer payloads. Revoke the old
key after the new key advances every required checkpoint. Roll back by restoring
the previous secret pointer and keeping the old key active only for the bounded
window. Then reconcile the denied tax-rate interval from the last successful
ERP cursor; do not full-import blindly.

## 3. Prepaid funding baselines

Follow `docs/runbooks/PREPAID_FUNDING_AUDIT_RESTORE.md`. Direct inserts, partial
cohort manifests, and hand-edited generated targets are forbidden. The current
legacy stock is remediation inventory; growth is the prevention failure.

Acceptance is a signed complete-cohort artifact, zero materializer blockers,
an audited materialization, and both the stock and growth signals returning to
zero. A later increase in `prepaid_funding_quarantined` is a new regression even
while an older cohort is still being reviewed.

## 4. Missing or stale prepaid billing anchors

The retired `backfill_next_billing_at.py` must not be used. It guessed cadence
and forgiveness from mutable/current facts. Preview the canonical evidence-based
repair instead:

```bash
poetry run python scripts/one_off/repair_stale_prepaid_billing_anchors.py --limit 500
```

The preview includes active prepaid subscriptions whose anchor is NULL or
behind an active entitlement. It proposes only the entitlement's exact coverage
end. Review the whole page and its fingerprint, then—on the separately approved,
explicitly named host—apply that exact fingerprint:

```bash
poetry run python scripts/one_off/repair_stale_prepaid_billing_anchors.py \
  --limit 500 \
  --apply \
  --reviewed-sha256 <exact-preview-fingerprint> \
  --actor <approved-operator-reference> \
  --reason <approved-change-reference>
```

Run until the evidence-backed cohort is zero. A NULL anchor with no exact active
entitlement is not repairable by this command: leave it in review, determine the
contract/lifecycle evidence, and use the owning lifecycle command. Do not infer
an anchor from account creation, current time, mutable offer cadence, or a
plausible invoice.

## 5. Receivables prioritization

The incident values (including the 60+ day stock and the late-August maturity
cliff) are point-in-time observations, not permanent repair targets. Rebuild the
queue at execution time from open invoice balance, immutable issue state, and a
verified due-date basis. Exclude `unknown_unverified` and legacy NULL bases from
age ranking and adverse collection action; a plausible shared due date is not
evidence.

Finance may rank the lawful remainder by age, value, customer commitment, and
contactability, but every action must enter the Collections owner and retain its
case result. Do not rank from an ERP copy until its feed checkpoints have been
reconciled, and do not use a bulk status/date edit to make invoices eligible.
Acceptance is a reproducible aggregate matching the work queue, an explicit
quarantine count, and a per-case outcome or next-action date.

## 6. Connected overdue accounts and dunning

Network use is impact/prioritization evidence, not proof that an invoice is
lawful or collectible. Recompute the cohort at execution time; the incident's
37 connected non-payers is not a hard-coded repair target.

After the accepted dunning-isolation and due-date-basis code is deployed, run a
read-only dunning preview through `financial.dunning`. A candidate must have an
open postpaid receivable with verified due-date basis and must pass the owner's
payment-arrangement, proof, extension, billing-profile, funding, and health
guards. `unknown_unverified` and legacy NULL due-date bases remain excluded.
Execute only owner-approved cases, preserving the per-account result and failure
audit. Access restriction must be requested through
`access.subscription_lifecycle` and verified by the RADIUS reconciler; do not
write account/subscription status or RADIUS rows directly. One failed account
must not roll back or stop unrelated cases.

Acceptance is an exact case/action trail for each acted account, an explicit
no-action reason for each excluded account, and converged access projection.
Rollback uses the dunning/access owner with the exact case evidence; it is not a
bulk status flip.

## 7. Historical July draft review stock

The period-aligned incident result—184 drafts totalling ₦7.59M—remains review
stock, not a collectible declaration. Recompute consumption against each
invoice's own service interval and preserve that evidence beside contract,
payment, entitlement, and lifecycle state. Current connectivity is only a
different proxy and must not replace period evidence.

For each draft, Finance chooses deliberate hold, void, or issue from lawful
source evidence. Issue/void only through `financial.invoices`; no bulk status or
timestamp edits. Issuance must create a complete immutable due-date basis. A
draft with ambiguous contract, service interval, account identity, prior
payment, or entitlement stays in review. The aged-draft stock remains visible,
but only the fixed recent creation cohort is a current billing-incident alert.

## 8. Shared phone numbers and payment attribution

Treat shared normalized identifiers as an identity review queue, not a dedupe
job. Export only aggregate counts and internal review IDs; raw phone/email
values must not enter logs or change artifacts. Customer Operations must prove
whether each shared value is a lawful organization contact, a household contact,
or a data error. Correct only the authoritative contact record, rebuild the
identity index through its owner, and verify that sensitive automation remains
blocked for every still-ambiguous value. Never assign an inbound payment from a
shared number by choosing the most recent or most active account.

## 9. Dormant subscriptions still marked active

No RADIUS session in 30 days is an observation, not a cancellation decision.
Recompute the cohort from period-aligned accounting evidence and keep accounts
with incomplete RADIUS ingestion, seasonal or backup service, an approved
service pause, or another known contract treatment out of any proposed churn
action.

Customer Operations must review the remaining account and contract evidence.
Cancel, suspend, pause, or retain service only through
`access.subscription_lifecycle`, with the effective date, reason, actor, and
customer or commercial evidence preserved. Do not directly change subscriber
or subscription status, and do not silently remove the cohort from
subscriber-count or ARPU reporting while the authoritative lifecycle still says
active. A future automatic inactivity policy requires a separately approved
contract defining the observation window, ingestion-freshness gate, customer
notice, exceptions, owner command, and reversal path. None is inferred by this
runbook.

Acceptance is a reviewed disposition for every candidate, a zero unexplained
dormant-active queue, and reporting that agrees with the resulting authoritative
lifecycle states.

## 10. The 22 August due-date cohort

Keep these invoices `unknown_unverified` and outside overdue selection,
Collections, and service restriction until Account Management provides source
evidence. The evidence must distinguish contract terms, an approved manual
override, and an unaudited bulk edit. A commercial decision needs its exact
contract/override reference; an operator error needs an audited correction
command and data repair. In neither case is a direct bulk date edit acceptable.

## 11. Empty WHT records

The ERP tax-rate scope does not explain an empty `withholding_tax_records`
table. Before calling it a defect, measure this like-for-like funnel for the
same interval:

1. customer tax policies with WHT enabled;
2. server-issued invoice-linked transfer intents carrying a positive WHT
   snapshot;
3. verified payment proofs with exact positive gross/net/WHT evidence;
4. succeeded payments linked to that proof evidence;
5. canonical WHT source records linked to those payments.

If stage 1 or 2 is zero, an empty source table can be expected configuration or
participation. If an eligible verified proof/payment exists and stage 5 is zero,
preserve the exact internal IDs and owner event/audit evidence, then diagnose the
`financial.payment_proofs` to `financial.tax_accounting` consequence. Do not
reconstruct WHT from ERP, invoice totals, narration, or a customer-entered rate.

## Completion record

For every executed item retain: accepted code digest when applicable, named
host, actor reference, time window, pre/post aggregate evidence, owner audit or
checkpoint ID, rollback result, and remaining review count. Exclude secrets,
raw contact identifiers, receipt recipients, and customer payloads.
