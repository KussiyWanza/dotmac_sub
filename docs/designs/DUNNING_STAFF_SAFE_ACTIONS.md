# Dunning Staff Safe Actions

Status: implemented

## Owners

`financial.dunning` remains authoritative for postpaid collection policy,
dunning-case lifecycle, canonical collectible receivable checks, action-log and
event evidence, account lifecycle projection, and service-access consequences.

`financial.dunning_staff_actions` owns the staff confirmation workflow for:

- pausing selected open cases;
- resuming selected paused cases;
- closing one open or paused case whose canonical collectible receivables are
  clear.

The coordinator does not reinterpret dunning eligibility. It receives an exact
impact preview from `financial.dunning`, locks selected cases and their accounts
in stable order, recomputes the preview, validates its fingerprint and explicit
confirmation, and stages every eligible transition plus audit evidence in one
transaction.

## Exact-scope contract

Missing selection never means the filtered cohort or every case. The preview is
bound to explicit selected case UUIDs and reports every row as eligible or
skipped with a reason. The fingerprint covers:

- action and exact deduplicated membership;
- case existence, lifecycle state, current step, and version;
- resulting state and eligibility;
- close-time collectible invoice count and per-currency receivables.

Confirmation processes the exact eligible subset shown in the preview. If any
selected row's membership, state, eligibility, or close-time receivable
evidence changes, the command rejects the whole confirmation and requires a new
preview. A staging or audit failure rolls back every selected transition.

## Consequence boundaries

Pause stops automated collection progression. Resume returns the case to active
collection progression. Close ends the case workflow only after collectible
receivables are clear.

Closing a dunning case is not service-restoration authority and never removes
an enforcement lock directly. Payment and billing reconciliation must
independently ask the existing financial-access consequence owner to release
only the exact holds whose current gates pass.

## UI migration

The list projects authorized page-only selection and pause/resume actions
through `ui.bulk_action_contracts`. Submission first renders an exact server
preview. The confirmation page uses `ui.action_form_contracts` to carry selected
IDs and the owner fingerprint and to require explicit confirmation.

Individual actions appear only on the case detail page and use the same shared
action contract. A close action with outstanding receivables is visible but
disabled with the owner-provided reason and per-currency amount.

Retired paths:

- direct list-row mutation forms;
- browser confirmation dialogs;
- web helpers that committed each case independently;
- exception swallowing and ambiguous partial success;
- post-commit audit writes;
- direct pause, resume, and close service methods used only by those web
  helpers.

## Verification

Focused tests cover exact membership, missing and ineligible rows, explicit
confirmation, stale previews, canonical close-time receivables, eligible-subset
execution, action logs, atomic audit, rollback, route cutover, template
architecture, and typed manifest integrity.
