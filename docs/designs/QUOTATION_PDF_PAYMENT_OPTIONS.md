# Quotation PDF Payment Options

Status: implemented source-of-truth contract.

## Decision

The immutable customer-facing quotation PDF includes one payment-presentment
snapshot directly after totals and before the existing footer. The rest of the
document remains unchanged. `sales.quote_documents` owns snapshot construction,
fingerprinting, rendering, artifact storage, and replay.

The snapshot contains:

- the primary enabled, complete bank destination selected for the quotation
  currency by `financial.collection_accounts`;
- the collection-account UUID as internal provenance;
- only Bank, Account Number, and Account Name as visible transfer fields; and
- an optional absolute URL built from the resolved company brand `app_url` and
  the company-hosted `/portal/quotes/{quote_id}/pay` route when the Quote has an
  exact Subscriber/customer portal identity.

No renderer-time lookup, legacy company/settings fallback, generic Paystack URL,
or public UUID bearer capability is permitted. Every active Quote can be
exported for staff review. A Lead-only Quote without a Subscriber identity
contains the complete commercial terms and bank-transfer destination but omits
the Paystack block. Missing bank details still fails creation closed; a linked
Quote also fails closed without a valid absolute company URL. A changed account,
identity-backed URL, or URL availability produces a different snapshot
fingerprint and artifact; an existing artifact remains immutable.

## Browser payment boundary

No compatible quotation browser route existed. The customer router therefore
adds a thin authenticated GET confirmation adapter plus POST initiation and GET
verification adapters:

- `GET /portal/quotes/{quote_id}/pay` authenticates, preserves the target across
  login, checks authorized subscriber ownership, active Draft/Sent state,
  expiry, unpaid state, positive authoritative deposit, and Paystack
  availability. It creates no invoice or intent.
- `POST /portal/quotes/{quote_id}/pay/intent` is CSRF protected by the global web
  boundary. It accepts only typed idempotency evidence, fixes the provider to
  Paystack, rechecks eligibility, and delegates to the established quotation
  deposit capability. Pending server-owned invoice intents replay instead of
  creating another checkout. Amount and currency never come from the URL, PDF,
  form, or JavaScript.
- `GET /portal/quotes/{quote_id}/pay/verify` rechecks authenticated ownership and
  delegates the Paystack reference to the established verification path.

The lifecycle remains quotation deposit initiation to issued invoice, canonical
Paystack verification and payment recording, then deposit-evidenced quotation
acceptance and order consequences. The browser adapters never write Quote,
Invoice, intent, Payment, or SalesOrder state directly.

`QuoteDepositInvoiceLink` is the structural Sale-to-Money identity between the
quotation and every deposit Invoice attempt. `TopupIntent.invoice_id` is the
structural checkout-to-document identity. Eligibility, replay, and verification
join through those foreign keys; JSON metadata remains non-authoritative
provider/reconciliation provenance. Migration 476 backfills both links from
legacy metadata only during the controlled schema migration. Ambiguous multiple
payable Invoice links fail closed rather than selecting one opportunistically.

## Artifact delivery

Admin download generates or reuses the content-addressed `QuotePdfExport` and
streams its stored file. Quote email delivery records the same export id, and
the communication attachment resolver streams that exact stored artifact. It
does not rebuild the PDF at send time.

Before queuing delivery, `sales.quote_delivery` consumes the typed
`sales.quote_payment_eligibility` query. The query rechecks exact Subscriber
ownership, active Draft/Sent state, expiry, paid deposit evidence, a positive
server-derived deposit amount, and installation-backed Paystack availability.
The email then reuses the immutable snapshot's exact HTTPS
`/portal/quotes/{quote_id}/pay` URL in both its HTML anchor and text/plain
alternative. It never sends an amount in the URL, calls Paystack directly, or
duplicates the protected POST initiation owner.

The branded email preserves the communication-intent, suppression, attachment,
audit, event, and idempotency workflow. Its subject comes from the snapshot's
legal company name; its customer name, Quote total, reference, primary colour,
application URL, logo, and support email come from the same owned Quote, Party,
and brand inputs used by the immutable document. Missing legal name or payment
eligibility fails the delivery command before an intent is queued. A suppressed
intent remains suppressed and never marks the Quote Sent.

## Preview and delivery boundary

An otherwise valid Lead/Party quotation without an exact Subscriber/customer
portal identity cannot safely enter the authenticated Paystack flow. It can be
exported while active for staff review, but its immutable snapshot stores a null
Paystack URL and the renderer omits the online-payment block. The implementation
does not assign another account, create a duplicate identity, or introduce a
public signed-payment-link contract.

Email delivery remains unavailable without exact Subscriber payment eligibility
and remains unavailable for Quotes with no positive authoritative
deposit, a paid deposit, an ineligible lifecycle state, expiry, or unavailable
Paystack capability. These are honest fail-closed states; the email does not
fall back to a generic provider URL or omit its mandatory payment action.
