# Deposit Account Credit

Sub owns this lifecycle. It is account credit, not a wallet, and it has no CRM
runtime dependency.

## Contract and owners

`financial.account_credit_deposits` owns eligibility, preview, typed intent,
provider correlation and atomic settlement. Customer gateway verification and
gateway reconciliation enter the owner-managed
`SettleAccountCreditDepositCommand` on a transaction-free session. Payment
webhook ingestion and payment-proof review already own wider evidence
transactions, so they compose the same typed `stage_verified_settlement`
participant, which flushes but never commits or rolls back. Callers cannot
select transaction behavior or pass a transport-shaped gateway object into the
domain owner.

Every new deposit intent persists:

- `purpose=account_credit_deposit`
- `allocation_policy=credit_only`
- `credit_application_policy=pay_eligible_invoices`
- `policy_version=1`
- the account, amount, currency, preview fingerprint, provider/reference,
  idempotency key, channel, creator and resulting payment link

Legacy payment intents remain untyped and retain their existing
invoice-first-then-credit behavior. They are not backfilled or reclassified.

`financial.payments` owns the confirmed payment, exact settlement, unallocated
credit evidence, refunds and reversals. The deposit command disables automatic
invoice allocation and prepaid-renewal drawdown, so the whole receipt first
becomes evidenced credit.

The receipt presents gross cash received separately from the settlement value
credited after provider fees, invoice applications, any settlement-owned
prepaid application recorded by the retired historical path, and the remaining
payment-backed credit. Those historical application rows remain only in
`payment_prepaid_applications_archive`; they are not a live decision or write
path. Migrations fail closed when that archive is missing, ambiguous, or does
not match its complete evidence schema. A receipt does not promise service
duration. When the downstream renewal owner actually funds a period it
publishes the exact `prepaid_service.renewed` outcome; portal and notification
views display that owner-provided renewed-through date.

Arbitrary account-credit deposits do not invent a WHT basis. The deposit owner
never calculates withholding tax from a deposit amount, never snapshots a
customer-entered WHT rate, and never turns an unallocated reseller or customer
credit into proof-backed WHT. Automatic WHT is reserved for invoice-linked
direct bank-transfer intents whose authoritative VAT-exclusive basis is owned by
the invoice and whose customer eligibility is owned by
`financial.customer_tax_policies`.

`financial.account_credit_applications` then locks the account, chooses eligible
invoices and payment-backed credit deterministically, and invokes the payment
allocation preview/confirmation owner. It never constructs allocation or ledger
rows itself. Invoice issuance and deposit settlement call the same applicator.

## Eligibility, preview and race policy

A customer may create a deposit for an active, non-disabled, non-cancelled
subscriber account, in NGN, inside configured limits, with no pending
account-credit deposit. Blocked or suspended billing accounts may deposit, but
the deposit alone does not restore access.

`financial.account_credit_deposits.active_request` is the customer-action read
owner for that pending-intent rule. It returns one typed active request with
observation and expiry times plus a closed phase/action pair:

- a pending direct transfer is `awaiting_receipt` / `upload_receipt`;
- another pending provider request is
  `awaiting_provider_confirmation` / `wait_for_provider`; and
- a submitted direct-transfer receipt is `under_review` / `wait_for_review`.

Expired and terminal requests are not active blockers. Customer adapters render
this result and disable a replacement deposit while it is active; they do not
filter `TopupIntent.status` or infer the next action independently.

The owner-generated preview is mandatory before checkout starts. For the exact
requested amount it reports:

- the requested deposit;
- every eligible invoice application in deterministic oldest-debt order;
- the total applied to invoices;
- the invoice amount still outstanding after application; and
- the remaining account credit after application.

The preview fingerprint includes the material allocation inputs: account,
requested amount, currency, current account credit, and the ordered eligible
invoice set with the balances and ordering facts that affect application.
Gateway and direct-transfer intent creation must present that reviewed
fingerprint back to the owner. If the reviewed preview is stale, intent
creation fails closed and the customer must review the updated preview first.

If an invoice appears after intent creation, confirmed cash is accepted and the
new credit is immediately applied to eligible invoices. Duplicate callbacks and
dead-letter replay return the existing payment. Provider amount, currency,
provider, exact intent correlation and account must match the server-owned
intent. Settlement sources use the closed owner vocabulary
`customer_gateway_verify`, `provider_webhook`, `gateway_reconciliation`, and
`payment_proof`; each command carries correlated command and idempotency
evidence into audit and the versioned deposit event.

Provider fee is typed settlement evidence, not a webhook-owned payment edit.
It must be between zero and the confirmed gross amount and is persisted by the
payment owner. Deposit credit remains the exact customer-authorized deposit
amount. When a gateway passes its fee to the customer, the verified gross charge
may exceed the authorized deposit only by that exact provider fee; the payment
owner preserves gross and fee evidence while crediting the authorized net
amount. Changing that policy requires a new owner contract and preview, not an
adapter-side net calculation.

Eligible invoices are active `issued`, `partially_paid` or `overdue` invoices
with a positive same-currency balance. Draft, void, written-off, inactive, and
incompatible-currency invoices consume nothing. Oldest due debt wins; creation
time and ID are stable tiebreakers. Partial credit leaves an invoice partially
paid. Only a fully paid invoice reaches the existing entitlement/access owner.

## Refunds, reversals, void and access

Existing payment refund and reversal owners remain authoritative. They use
append-only evidence, consume unallocated credit first, reopen affected
receivables when allocated cash is returned and hand access state back to the
canonical access owner. Invoice void composes the account-credit owner to retire
typed account-credit allocations, append exact ledger reversals and restore the
same payment-backed credit. Other direct-payment or credit-note settlement must
still be reversed through its own owner before voiding. No balance counter is
edited.

## ERP accounting projection

ERP continues to pull the bounded payment sync feed. Deposit payments now carry
their settlement plus the typed intent purpose/policies. Later account-credit
allocations touch the parent payment watermark and include the exact invoice
credit and account-credit-consumption links, so incremental ERP sync observes
both stages.

Expected ERP journals are:

- Deposit confirmed: Dr Cash / provider clearing; Cr Unapplied customer credit
  liability.
- Credit applied: Dr Unapplied customer credit liability; Cr Accounts
  receivable.

Invoice revenue and tax recognition remain invoice-issuance concerns. ERP owns
the journals; Sub owns the customer receipt, credit and allocation evidence.
An ERP projection failure must be retried downstream and never rolls back Sub's
settlement.

## Operations

`AccountCreditApplications.inspect_invariants` is read-only and its aggregate
count is published by the existing billing-health snapshot. It reports payable
invoices with unused compatible credit, overallocated payments, completed
deposit intents without exact settlement evidence, duplicate provider
references and unresolved deposit webhooks. Repair invokes the canonical
applicator; it never invents payments or infers cash from memo text.

Customer-facing pages say “Deposit Account Credit”, show current credit, show
the live owner-generated allocation preview, and permit checkout even when
payable invoices exist. Those pages must not compute invoice allocation locally
or offer customer-controlled allocation behavior. An unresolved balance still
renders as unavailable rather than zero.
