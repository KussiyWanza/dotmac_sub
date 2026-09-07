# Mobile Billing Document Details

Status: implemented

## Page contract

- Audience and task: an authenticated subscriber reviews an exact invoice,
  payment, or immutable account-ledger entry and downloads the corresponding
  canonical financial document when one exists.
- Authority: `financial.invoices`, `financial.payments`, and `financial.ledger`
  own the displayed facts. `app.services.billing_invoice_pdf` owns invoice PDF
  rendering, freshness, storage, and filenames. The established payment-receipt
  service owns successful-payment receipt rendering. Self-care API routes and
  Flutter screens are adapters and never infer financial state.
- Scope: every detail and document request derives the subscriber account from
  the authenticated principal. A missing record and a record owned by another
  account both return the same not-found response.
- First viewport: document identity, authoritative amount, status, and event
  date precede secondary reference, fee, refund, memo, and allocation details.
- Actions: invoice download is a secondary action beside the existing payment
  action. Payment receipt download is enabled only for a server-reported
  successful payment. Activity rows always open their immutable ledger detail;
  related invoice and payment links appear only when their IDs are present.
- Document delivery: the mobile client requests authenticated PDF bytes,
  requires the `application/pdf` content type and `%PDF-` signature, writes
  only to temporary app storage, and opens the native save/share sheet. It does
  not expose bearer tokens in browser URLs or generate financial PDFs locally.
- States: loading, not found/unauthorized, temporarily unavailable PDF, invalid
  document response, native-sheet failure, and successful handoff remain
  distinct. Repeated taps are disabled while a document is being prepared.
- Responsive and accessible behavior: screens use vertical disclosure,
  selectable references, labeled PDF actions, status text in addition to
  color, chevrons for tappable rows, and standard Material touch targets.

## API surface

- `GET /api/v1/me/invoices/{invoice_id}/pdf`
- `GET /api/v1/me/payments/{payment_id}`
- `GET /api/v1/me/payments/{payment_id}/receipt/pdf`
- `GET /api/v1/me/ledger/{entry_id}`

All four routes are read/self-care adapters over existing owners. They create
no payment, allocation, invoice, or ledger state.

## Verification

- API unit tests prove account scoping and canonical document delegation.
- Flutter repository tests prove authenticated paths, byte response mode,
  filename handling, and PDF MIME/signature rejection.
- Flutter model and widget tests prove typed detail parsing and exact payment
  and Activity row navigation.
