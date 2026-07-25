# Payment Arrangement Safe Actions

Status: implemented

## Boundary

`financial.payment_arrangements` remains authoritative for arrangement
eligibility, lifecycle state, installment schedules, payment application, and
collection-shield facts.

`financial.payment_arrangement_staff_actions` owns the staff confirmation
workflow for:

- approving a pending arrangement;
- canceling a pending or active arrangement;
- recording the next owner-selected installment as externally paid.

The coordinator does not independently infer eligibility or choose an
installment. It obtains a preview from `financial.payment_arrangements`, locks
the arrangement and active installments, recomputes that preview, validates its
fingerprint and explicit confirmation, asks the arrangement owner to stage the
transition, and stages audit evidence in the same transaction.

## Preview contract

The preview binds:

- action and arrangement identity;
- current and resulting lifecycle state;
- installment progress and schedule state;
- the exact target installment for manual payment recording;
- currency and amount facts;
- collection-shield consequence;
- a deterministic state fingerprint.

Any changed arrangement or installment evidence invalidates the preview.
Adapters return a conflict and render a freshly projected action instead of
guessing whether the old command remains safe.

## UI contract

`ui.action_form_contracts` carries the preview fingerprint as an
owner-produced hidden value and renders confirmation as a required, labeled
checkbox. Browser confirmation dialogs are retired. The payment-arrangement
template receives server-formatted details, status presentation, installment
rows, and action forms; it contains no status-to-action decision, money
arithmetic, or page-local mutation JavaScript.

Manual installment recording is explicitly evidence-only. It does not create a
canonical billing `Payment` document or ledger transaction. The impact copy
states that boundary before confirmation.

## Migration

- Old owners: admin routes and web helpers committed transitions; audit was
  written after the state commit; Jinja selected actions from raw status and
  used browser dialogs.
- New owner: `financial.payment_arrangement_staff_actions`, coordinating
  participant mutations from `financial.payment_arrangements` and staged audit
  persistence from `observability.audit_log`.
- Cutover gate: every staff action carries the current preview fingerprint and
  explicit confirmation, and the locked command rechecks both eligibility and
  target evidence.
- Retired fallback: direct admin lifecycle helpers, post-commit audit, raw
  arrangement action forms, and browser confirmation dialogs.
- Verification: focused command, rollback, stale-preview, action-projection,
  accessibility, template architecture, and SOT-manifest tests.

Customer arrangement creation/cancellation, automatic payment application, and
scheduled overdue/default processing remain separate existing entrypoints under
`financial.payment_arrangements`; this slice does not alter their policy.
