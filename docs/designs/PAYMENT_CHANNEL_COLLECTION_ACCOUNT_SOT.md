# Payment channels and collection accounts — source-of-truth consolidation

Status: accepted architecture; A1 implemented locally on
`feat/payment-channel-collection-account-sot`, pending PostgreSQL/CI and deployed
cutover verification, 2026-07-20.
Owner: `financial.collection_accounts`.

Revision note: an earlier draft treated this as one linear project and assumed the
customer-facing payment path ran on `payment_channels`. **It does not.** A deeper
review found two unconnected subsystems and four copies of the company bank
account. This revision splits the work accordingly. See "Architecture constraint".

## Problem

"Which Dotmac bank account received this money?" is unanswerable, because the
account shown to a customer is **not an entity** — it is a dict parsed out of a
JSON settings string. There is no identity to record on the payment.

**Four copies of the same fact — the company's receiving bank account:**

| # | Where | State |
|---|---|---|
| 1 | `billing.direct_bank_transfer_accounts` (JSON string in `domain_settings`) | **Frozen rollback snapshot** — 2 Zenith accounts, full numbers; no runtime reader or writer after A1 |
| 2 | `billing.direct_bank_transfer_bank_name` / `_account_name` / `_account_number` / `_sort_code` | Legacy singular settings, same fact |
| 3 | `company_bank_name` / `company_bank_account` (`web_system_company_info`) | Fallback branch of `invoice_bank_details.get_invoice_bank_details()` |
| 4 | `collection_accounts` table | **Owner** — production identities are seeded; A1 adds full presentment details |

`invoice_bank_details.py`'s docstring calls copy 1 "the source of truth", so the
drift is written down as if it were the design.

**And the attribution chain breaks at every hop:**

| Concern | Where | State |
|---|---|---|
| Channel of a payment | `payments.payment_channel_id` | Production backfill complete for all evidence-supported rows |
| Account of a payment | `payments.collection_account_id` | Populated only where evidence names a specific destination |
| Account a customer chose | `payment_proofs` | free-text `bank_name` only (301 rows) |

Note `direct_bank_transfer_enabled` is a **policy toggle, not a duplicated fact**.
It is legitimate configuration and is out of scope for deduplication.

## Architecture constraint — two subsystems that must stay separate

**Presentment — "how can this customer pay right now?"**

- Online gateways: `payment_routing.gateway_options()` projects enabled
  connector capability bindings in explicit presentment-priority order. See
  `PAYMENT_GATEWAY_CONTROL_PLANE_SOT.md`.
- Direct bank transfer: appended by `_topup_payment_options()` when enabled, with
  accounts read from `financial.collection_accounts` through the owner reader.
- Consumers: customer portal top-up, reseller portal (`web_reseller_billing.py:81`),
  `api/me.py:703`.

**Attribution — "where did this money land?"**

- `payment_channels` + `collection_accounts` + `payment_channel_accounts`, via
  `_resolve_payment_channel` / `_resolve_collection_account`, on payment
  create/edit and consolidated payments.

**These two do not share runtime decisions.** Gateway setup may ensure the
provider-linked channel needed for attribution, but the customer-facing path
does not consult `payment_channels`.

> **Rule: do NOT route presentment through `payment_channels`.** Gateway
> presentment stays with `payment_routing`, which is installation-aware and
> policy-ordered. Channels have neither. Channels remain
> the classification dimension for *recorded* payments and reporting.

What the two subsystems legitimately share is the **bank account identity**. That
is the seam, and unifying it is what makes per-account reconciliation possible.

## Principles

1. `collection_accounts` is the **single owner** of "a Dotmac bank account".
   Copies 1–3 are **retired, not mirrored**. During A1 deployment, their stored
   rows remain briefly as a frozen rollback snapshot with no runtime writer or
   reader. They are deleted after verification; they are never a live fallback.
2. One channel, many accounts. The fan-out lives in `payment_channel_accounts`.
   **Never create one channel per bank.**
3. A payment records both its channel and its collection account. Reconciliation
   reads columns, never memo strings.
4. Backfill asserts an account only where source evidence names one. Ambiguous
   rows stay NULL — a wrong account is worse than a missing one.
5. Sub carries accounting **mapping codes**, never a ledger (see below).

## Decisions

- **D1 — accepted.** Copies 1–3 are deleted in the contract phase after deployed
  verification. Until then they are a frozen rollback snapshot, not an alternate
  authority.
- **D2 — accepted.** Sub carries nullable `accounting_code` mappings and has no
  chart of accounts or runtime dependency on dotmac_erp.
- **D3.** Confirm the account list in B1 is complete, and which accounts are open
  for new payments versus historic-only. Full account numbers exist today only for
  the two Zenith accounts.

---

# Workstream A — Bank account consolidation

Well-bounded, high value, and the prerequisite for invoice display, customer
routing and per-account reconciliation. Independent of workstream B.

## A1 — Make `collection_accounts` the owner

1. Migration: add `account_number`, `account_name`, `sort_code`, nullable
   `accounting_code`, and explicit `presentment_priority` to
   `collection_accounts`. Add `accounting_code` to `payment_channels`.
2. Migrate the two live Zenith accounts out of copy 1 into the table.
3. Repoint every consumer at the owner (blast radius, ~8 sites):
   - `customer_portal_flow_payments`: `enabled_direct_bank_transfer_accounts`,
     `direct_bank_transfer_accounts`, `direct_bank_transfer_settings`, and the
     options builder
   - `web_reseller_billing.py:81`
   - `api/me.py:703`
   - `invoice_bank_details.get_invoice_bank_details()` — and **delete its
     company-info fallback** (copy 3)
   - `billing_invoice_pdf.py` (3 call sites), `web_billing_invoices.py:747`
   - `web_system_config.py` + admin `system.py` settings UI → point staff at the
     existing collection-accounts CRUD instead
4. Expand/contract rollout: stop every runtime read/write of settings copies 1–3,
   retain the rows/specs only through the verification window, then delete them.
   Keep `direct_bank_transfer_enabled` and `_instructions` — policy and copy, not
   duplicated bank-account facts.
5. Architecture test: nothing outside the collection-account owner reads
   `direct_bank_transfer_accounts` or the company-info bank fields, and sub defines
   no chart-of-accounts model.

### A1 cutover gates

1. Migration fails closed on invalid JSON, incomplete facts, duplicate legacy
   identities, or ambiguous last-four matches. It enriches the existing
   Splynx-backed identity where unique and inserts a deterministic owner row where
   no identity exists.
2. Every migrated live account has `bank_name`, `account_name`, `account_number`,
   derived `account_last4`, currency, active state, and explicit presentment order.
3. Portal, reseller portal, `/api/me`, invoice web view, and new invoice renders
   show the owner-backed accounts. New quotation snapshots also select the
   typed primary account for their currency and retain its identity as internal
   provenance while displaying only Bank, Account Number, and Account Name.
   Archived invoice and quotation exports remain unchanged.
4. Do not edit collection-account payment details during the rollback soak: old
   code can read only the frozen snapshot. After verification, remove the snapshot
   and its specs in the immediate contract change.

**Invoice PDF risk is low.** PDFs are archived (`InvoicePdfExport`,
`get_latest_export`), so historical documents are stable artifacts and only new
renders pick up the changed source. Confirm re-render behaviour before cutover.

Never store customer payment credentials here. These are Dotmac's own receiving
accounts, which the business publishes.

## A2 — Customer-type routing

`_resolve_collection_account(db, channel, currency, collection_account_id)` never
sees the subscriber, so segment routing is impossible today.

1. Promote `SubscriberCategory` from `metadata_` JSON to a real column —
   prerequisite for both routing and per-segment reporting.
2. Add a segment dimension to `payment_channel_accounts`.
3. Extend the resolver to accept the subscriber, preserving current behaviour when
   no segment-specific mapping exists.

`Subscriber.reseller_id` is already a column and can route without step 1.

## A3 — Invoice payment surface

Receipt upload is **already fully built for prepaid top-ups**: model
(`app/models/payment_proof.py`), API (`app/api/payment_proofs.py`), admin review
(`billing_payment_proofs.py`), customer flow (`/portal/billing/topup/transfer`),
templates, and 301 proofs in production. Invoices already print *one* bank account
via `invoice_bank_details`, but `invoice.html` offers only a bare "Pay" link and
`pay.html` is Paystack-only — no channel choice, no receipt upload.

Work: extend the invoice view with a channel choice; for Bank Transfer, render the
applicable accounts from `collection_accounts` and reuse the existing proof upload
scoped to an invoice rather than to account credit. New surface over existing
machinery, not new machinery.

---

# Workstream B — Channel attribution

Independent of workstream A. The seeded channels and the Splynx backfill evidence
need none of the presentment work.

## B1 — Complete the channel and account seed

The 2026-07-20 seed created four channels (Paystack, Flutterwave, Bank Transfer,
Cash). The authoritative Splynx `payments_types` list shows it is **incomplete**.

**Channels** (`payment_channels`)

| Channel | type | provider | notes |
|---|---|---|---|
| Paystack | other | paystack | seeded |
| Flutterwave | other | flutterwave | seeded |
| Bank Transfer | bank_transfer | — | seeded; fans out to accounts below |
| Cash | cash | — | seeded |
| Remita | other | — | **add**; no provider record yet |
| Card | card | — | **add**; Splynx "Credit card" |
| Other | other | — | **add**; explicit catch-all, not a silent NULL |

**Collection accounts** (`collection_accounts`)

| Account | type | currency | Splynx label |
|---|---|---|---|
| Zenith …6461 | bank | NGN | `Zenith 461 Bank` |
| Zenith …9523 | bank | NGN | `Zenith 523 Bank` |
| UBA | bank | NGN | `UBA` |
| Dotmac USD | bank | USD | `Dotmac USD` |
| Cash — CBD | cash | NGN | `Cash CBD` |
| Cash — general | cash | NGN | `Cash` |

Map accounts to channels via `payment_channel_accounts`, using its `currency`
column for the USD account so `_resolve_collection_account` selects it correctly.

**Resolver hazard:** `_resolve_payment_channel` raises HTTP 400 ("Multiple payment
channels match provider; set a default") when a provider has more than one active
channel and none is default. Keep exactly one channel per provider, explicitly
defaulted. `is_default` is scoped per provider, so defaults across providers do not
collide.

## B2 — Close the attribution chain

1. Add a collection-account reference to `payment_proofs` (the customer already
   selects one; the template posts `account.id`, it is simply not persisted).
2. Carry it through proof verification to `Payment.collection_account_id`.
3. Ensure every payment-creating path sets channel and account, including the
   Paystack webhook (channel resolves from `provider_id` today) and manual admin
   capture.
4. Test: a verified bank-transfer proof produces a payment whose channel is Bank
   Transfer and whose collection account is the one the customer chose.

## B3 — Backfill history from Splynx

**Migration completeness verified 2026-07-20:** the Splynx `payments` table holds
**98,417 rows** (AUTO_INCREMENT 98,418), counted directly from the seabone dump;
production carries **exactly 98,417** payments with a `splynx_payment_id`. No
payments were lost. The remaining 764 of 99,181 are native.

Evidence available on production: `splynx_billing_transactions` 232,377 rows,
`splynx_id_mappings` 240,027. **98,413 of the 98,417 join** — 4 payments carry a
Splynx id with no evidence row and must fall back to native evidence or stay NULL.

`category_name` is uniformly "Payment" and carries nothing, but `description` is a
closed set naming **both channel and specific account** (these are
`payments_types.name`, so the mapping is authoritative, not inferred):

| Splynx description | n | amount | → channel | → account |
|---|---:|---:|---|---|
| Paystack | 64,562 | 1,755,971,060.78 | Paystack | — |
| Zenith 461 Bank | 25,938 | 1,627,886,648.31 | Bank Transfer | Zenith …6461 |
| Bank transfer | 2,458 | 319,751,040.07 | Bank Transfer | *(unnamed — leave NULL)* |
| Zenith 523 Bank | 1,838 | 407,930,579.24 | Bank Transfer | Zenith …9523 |
| Other | 1,308 | 37,692,686.75 | Other | — |
| Cash CBD | 1,193 | 55,286,385.00 | Cash | Cash — CBD |
| Flutterwave | 669 | 15,354,488.00 | Flutterwave | — |
| Cash | 338 | 17,116,133.87 | Cash | Cash — general |
| Credit card | 75 | 7,329,625.03 | Card | — |
| Dotmac USD | 16 | 10,679,738.08 | Bank Transfer | Dotmac USD (USD) |
| UBA | 16 | 917,500.00 | Bank Transfer | UBA |
| Remita | 2 | 417,000.00 | Remita | — |

Channel derivable for **100%** of joined rows; account for **29,299**. The 2,458
generic "Bank transfer" rows get a channel but **no account** — the honest outcome,
not a defect to paper over.

The 764 native payments backfill from native evidence: `provider_id` where set
(217 rows), else memo prefixes (`Paystack prepaid top-up ref: DMAC-`,
`Bank transfer (proof …)`, `NIP/FBN/…`, `TRF FROM …`); 453 of 668 post-cutover
payments carry an `external_id`.

Execution: one-off script, dry-run by default, emitting a per-bucket diff and an
explicit unmatched count. Never overwrite a non-NULL channel or account.

## B4 — Reports and filters

Only meaningful after B1 and B3, or reports show channels with no volume.

- Payments by channel, and by collection account, over a period.
- Per-account reconciliation against bank statements; per-gateway against
  settlement reports once `fee_rules` and accounts are populated.
- Gross versus settled once fees are modelled.
- Channel and account as filter facets on the payments list, following the existing
  list-projection pattern.
- `reporting.py:827` already outer-joins `PaymentChannel` but groups by
  `method_key`; replace with a real channel dimension.

---

# Accounting-integration scope (app-independent)

Sub and ERP are **separate applications**. Sub must not depend on ERP, and must not
grow a general ledger of its own. The goal is narrower: carry just enough stable
reference data that **any** accounting app — QuickBooks, Xero, Sage, Zoho, or
dotmac_erp — can be integrated later without reshaping billing.

**In scope for sub**

- A nullable `accounting_code` on the objects an accounting export must classify:
  `collection_accounts` (where money landed) and `payment_channels` (how it
  arrived). Free-form string, owned as configuration, meaningful only to the
  external system.
- The same treatment for revenue/receivable classification, reusing the existing
  `LedgerCategory` rather than inventing a parallel taxonomy.
- Editable through the existing admin CRUD; exposed on the payment/collection read
  models so an exporter can consume it.

**Explicitly out of scope for sub**

- Chart of accounts, account categories, account balances.
- Journals, journal entries, GL postings, trial balance.
- Bank accounts as financial entities, and any FK or runtime call into ERP.

**Precedent:** Splynx did exactly this — `payments_types` (operational channel)
referenced `accounting_bank_accounts`, a thin table of `accounting_id` + `name`
(`Zenith 461 Bank` → `397`, `Zenith 523 Bank` → `395`, `Paystack` → `398`,
`Cash CBD` → `342`, `Undeposited Funds` → `425`). Those are QuickBooks GL codes
held as *mapping*, not as a ledger. Splynx modelled no accounts, journals or
balances and still integrated Xero, SageOne and QuickBooks. Those existing codes
are a ready-made seed for the mapping.

If more than one accounting system must be supported simultaneously, promote
`accounting_code` to a small mapping table keyed by (entity, system, code). Start
with the single field; add the table only when a second system actually exists.

---

# Sequencing

**A1 first** — it collapses four authorities into one and is what makes everything
downstream possible. A2 and A3 follow it.

**Workstream B runs in parallel.** B1 and B3 depend only on each other, not on A.
B2 is the join point: it needs A1's owned accounts to reference.

B4 last — reporting before backfill shows empty channels. A2 needs the
`SubscriberCategory` column promotion first, or it encodes segment logic against a
JSON field.
