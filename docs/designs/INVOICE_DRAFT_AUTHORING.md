# Invoice Draft Authoring Boundary

## Decision

`financial.invoice_draft_authoring` owns administrative creation and editing of
the complete invoice draft aggregate, plus administrative conversion of a
proforma into a final invoice. The aggregate includes the invoice header, all
active lines, owner-derived totals, create or conversion idempotency evidence,
audit evidence, and transactional invoice outbox events.

The admin web route is a parsing and error-mapping adapter. It must release any
read transaction before invoking `create_invoice_draft`, `update_invoice_draft`,
or `convert_proforma_invoice`. It does not create an invoice header, mutate
invoice lines, derive conversion status, recalculate totals, commit, or publish
notifications itself.

## Invariants

- Administrative authoring always produces or edits `draft`; issue, announce,
  void, write-off, settlement, and reconciliation use separate named commands.
- A draft contains at least one valid line.
- The account is locked before the invoice and its lines.
- Header, lines, totals, audit evidence, idempotency evidence, and event staging
  commit once or roll back together.
- Issued and terminal invoice documents cannot be edited.
- Proformas remain drafts, cannot consume account credit, are excluded from
  collectible AR/dunning, cannot be paid, and cannot be announced.
- Conversion locks the account before the invoice, accepts only a current draft
  proforma, durably binds one deterministic retry key to that invoice, and
  applies canonical account credit before deriving the committed final status.
- Generic conversion rejects prepaid accounts and any proforma linked to a
  prepaid subscription before reserving idempotency evidence or moving money.
  Those documents require the reviewed prepaid proforma adoption and draft
  reconciliation workflow.
- A duplicate conversion request replays the first result. It cannot overwrite a
  concurrent or already committed `paid` status with `issued`.
- `invoice_created` for a draft is internal evidence and does not request a
  customer notification. An explicit final issue/send event carries
  `invoice_number`, `amount`, and `due_date`.

## Migration

The retired admin path committed the invoice header and then called independent
invoice-line writers, each of which committed separately. The replacement
admits one typed complete-state command through `execute_owner_command`.
The shared billing adapter's CRM/subscription invoice-with-lines path now also
delegates to the invoice owner's single-commit constructor; account credit and
the created event are applied only after its complete line set and totals exist.

The retired proforma conversion path read an unlocked invoice, prepared an
`issued` update from that snapshot, and committed after payment allocation could
have marked the same invoice `paid`. A client disconnect followed by a retry
could therefore repeat conversion and restore stale `issued` state. The typed
conversion command now serializes conversion with payment through the canonical
account and invoice locks, makes retries idempotent, and reapplies available
credit within the same owner transaction.

Generic conversion is intentionally not a prepaid repair path. A prepaid
proforma can lack the subscription and service-period identity needed to create
entitlement and advance the billing anchor. The owner therefore fails closed
and directs the operator to the dry-run-first prepaid reconciliation owner.

Historical prepaid drafts and other pre-existing ambiguous billing records are
not modified by this prevention change. They require a separately reviewed,
dry-run-first reconciliation command with deterministic repair evidence.
An exact already-paid, periodless prepaid document uses that separate
historical command; generic conversion remains prevention-only and never
creates entitlement or restores access.

## Verification

Focused behavior tests cover successful atomic creation, rollback after staged
lines, idempotent replay, draft-only updates, non-payable drafts/proformas,
concurrent payment/conversion status preservation, and complete notification
event context. Architecture tests pin the owner contract and prevent the admin
adapter from returning to direct invoice/line writers or unlocked conversion.
