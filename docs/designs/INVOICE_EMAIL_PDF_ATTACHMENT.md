# Invoice Email PDF Attachment

Status: implemented

## Ownership

- `financial.invoices` owns invoice identity, lifecycle, account scope, and send
  eligibility.
- `app.services.billing_invoice_pdf` owns canonical invoice rendering, cache
  freshness, storage, and filename generation.
- `communications.intents` owns the durable attachment reference and delivery
  expansion.
- The notification worker materializes the reference immediately before
  transport. SMTP owns MIME encoding only.

The event and notification records never contain PDF bytes or a storage path.
They retain an allowlisted `invoice_pdf` descriptor with the invoice UUID,
canonical filename, and MIME type. This makes retries durable without creating
a second invoice-document authority.

## Delivery contract

Only the email channel for `invoice.sent` receives an invoice-PDF descriptor.
On the admin invoice detail page, **Issue & send invoice** performs the guarded
draft-to-issued transition and stages `invoice.sent` in that same invoice-owner
transaction. **Send invoice** on an already-issued document stages the same
event without another lifecycle transition. Proformas and terminal void or
written-off documents remain unsendable.

The stock email greets the customer, explains that the attached invoice is for
review regarding their service, and includes the invoice number, amount, and
due date. The exact historical stock template is migrated to this copy while
operator-customized templates remain untouched.

At delivery time the worker:

1. parses the invoice UUID;
2. verifies the invoice is active, final, and owned by the notification's
   Subscriber account;
3. reuses a fresh canonical PDF export or generates it through
   `billing_invoice_pdf.generate_export_now`;
4. streams and validates the PDF signature and ten-megabyte size limit; and
5. sends one `multipart/mixed` message containing the existing plain/HTML
   alternative and the PDF attachment.

The generated document is therefore the same branded/template-backed PDF used
by admin and customer portal downloads.

## PDF payment presentment

Current invoice renders first read the customer account `payment_method`.
`transfer` and `bank_transfer` print bank details; `paystack` and card-like
values print the Paystack link. Blank customer methods and accountless renders
fall back to `billing.invoice_pdf_payment_presentment` from the settings owner.
The closed fallback values are `bank_account`, `paystack`, and `both`; the
default is `paystack` so fresh or unconfigured deployments print the stable
invoice-scoped Paystack payment link.

Bank-transfer details are resolved from `financial.collection_accounts` through
`invoice_bank_details.get_invoice_bank_details()`. The PDF never stores or
copies bank account facts onto the invoice. Paystack presentment uses the same
stable invoice-scoped checkout hand-off at `/pay/invoices/{invoice_id}` that
rechecks payable state and creates a fresh checkout intent before redirecting
to Paystack. The PDF stores only the stable Dotmac hand-off URL, never an
expiring provider authorization URL. The normal signed webhook/reconciliation
path remains the settlement authority.

Changing the invoice template freshness marker invalidates cached exports so
new downloads and invoice email attachments do not retain the previous payment
surface. A later customer payment-method change or operator change to
`invoice_pdf_payment_presentment` also makes older cached exports stale against
that decision timestamp.

## Failure and retry

A missing, invalid, stale/unavailable, oversized, or account-mismatched PDF is a
delivery failure. The existing notification retry lifecycle retries the whole
email. Sending the email body without its required invoice is forbidden.

Stable failure evidence is recorded without PDF content or unnecessary customer
data. Attachment bytes exist only within the delivery process.

## Verification

- Communication-intent tests prove the typed reference is durable.
- Resolver tests prove canonical export reuse, PDF validation, filename safety,
  and account-scope failure.
- Email tests prove the MIME structure preserves text and HTML alternatives and
  adds the PDF as an attachment.
- Invoice-send tests continue to prove the web adapter delegates to the canonical
  invoice announcement owner.
