# Duplicate Payment-Proof Correction

Status: implemented design contract

Owner: finance operations

## Intent

Correct the exceptional case where staff verified a second uploaded proof for
cash that an earlier verified proof had already recorded. The workflow must
remove only the duplicate financial value without rewriting either historical
review or presenting the correction as a bank chargeback.

## Authority

`financial.payment_proofs` owns:

- eligibility of the duplicate and retained-original proof pair;
- operator-supplied correction evidence;
- the preview fingerprint;
- the append-only `PaymentProofCorrection` record;
- the correction audit and `payment_proof.corrected` event.

`financial.payments` remains the only owner of:

- payment-reversal eligibility and locking;
- payment status;
- customer and account-credit ledger effects;
- allocation and invoice consequences;
- payment-reversal evidence, audit, and funding-change events.

The proof owner composes the payment reversal with `commit=False` inside its
owner-managed transaction. Routes and templates never write proof, payment,
invoice, balance, or ledger state.

## Eligibility and failure behavior

The workflow fails closed unless:

- both proof identities are distinct, verified, and linked to payments;
- both target the same subscriber account, currency, and verified amount;
- neither selected proof was already corrected as a duplicate;
- the retained original payment still has settled value;
- the duplicate payment is eligible for a canonical manual reversal;
- the confirmation fingerprint still matches the locked proof and financial
  state.

Consolidated reseller proofs are excluded because WHT and billing-account
return evidence require the consolidated-payment owner workflow.

The UI offers same-account, same-currency, same-amount verified proofs as
candidates only. Candidate projection is not a duplicate decision: the
operator must compare both receipts and bank evidence before confirmation.

## Transaction and evidence

Confirmation starts on a transaction-free adapter session. The proof owner:

1. resolves an idempotent replay by stable command key;
2. locks both proof rows in UUID order;
3. rebuilds and compares the proof-correction and payment-reversal previews;
4. delegates the exact reversal to `financial.payments`;
5. writes one `PaymentProofCorrection` linked to the duplicate proof, retained
   original proof, duplicate payment, payment reversal, and ledger entry;
6. stages actor audit and `payment_proof.corrected` event evidence;
7. commits all effects once at the public owner boundary.

Database uniqueness prevents more than one correction per duplicate proof,
payment reversal, or correction idempotency key.

## UI page contract

Screen: admin payment-proof detail and duplicate-correction confirmation.

Audience: finance staff holding both `billing:proof:verify` and
`billing:payment:update`.

Decision: identify which earlier verified proof remains authoritative and
confirm the exact reversal of the later duplicate payment.

The detail page shows the action only when the proof owner supplies eligible
original candidates. The confirmation page displays:

- duplicate and retained-original proof identifiers;
- duplicate and retained-original payment identifiers;
- reversal amount and currency;
- before/after prepaid funding and account credit;
- invoice-effect count;
- operator reason and immutable preview fingerprint.

After completion, the proof detail shows the effective “Corrected duplicate”
state and links the original proof, correction, reversal, and ledger evidence.
Unauthorized users receive no action and the command rechecks both permissions.

## Validation

Focused tests cover eligibility, stale confirmation, idempotent replay,
append-only evidence, exact reversal delegation, audit/event evidence,
permission enforcement, route delegation, and template disclosure.
Architecture tests prevent routes or templates from owning financial writes.
