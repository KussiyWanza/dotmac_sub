# Payment Configuration Settings Safe Actions

Status: implemented

## Ownership

`financial.payment_configuration_staff_actions` owns reviewed staff lifecycle
and default-selection decisions for `CollectionAccount`, `PaymentChannel`, and
`PaymentChannelAccount`.

`financial.collection_accounts` continues to own receiving-account identity,
payment details, presentment order, and lifecycle storage.
`financial.payment_routing` continues to own connector-backed checkout gateway
eligibility and selection. Payment channels and account mappings classify
recorded settlement; they do not route customer checkout.

## Canonical admin surface

- `/admin/settings/billing/collection-accounts`
- `/admin/settings/billing/payment-channels`
- `/admin/settings/billing/payment-channel-accounts`

There are no compatibility redirects from the former `/admin/billing/...`
configuration routes. New records are staged inactive. Create/edit forms may
change descriptive configuration only; lifecycle and default decisions use a
server-owned impact preview and explicit confirmation command.

## Command invariant

Confirmation locks the affected configuration, rebuilds the preview, compares
the exact fingerprint, verifies eligibility, stages the state mutation and
audit event, and commits once through `execute_owner_command`. Stale previews
fail closed and return to review.

Deactivating a collection account also deactivates its active attribution
mappings, but never rewrites historical payments. The last active destination
for a currency cannot be deactivated. An inactive mapping cannot become the
default, and a default mapping with active peers requires a reviewed
replacement before deactivation.

## Retired paths

Migration `418_payment_channel_mapping_sot` projects the legacy
`PaymentChannel.default_collection_account_id` into
`payment_channel_accounts`, then removes the duplicate column. The payment
resolver reads mappings only. Direct toggle routes, lifecycle/default
checkboxes, editable derived account suffixes, generic lifecycle/delete API
fields, and browser confirmation handlers are removed from this surface.
