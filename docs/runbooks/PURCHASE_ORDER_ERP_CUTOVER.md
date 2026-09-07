# Purchase Order ERP Cutover

Owner: `integration.procurement_purchase_order_cutover`

This runbook moves purchase-order and purchase-invoice origination from their
retired predecessor to Selfcare and stages a bounded set of approved historical
quotes. It is a production financial control, not a generic backfill utility.

## Preconditions

1. Name the Selfcare and ERP hosts and record UTC start time, deployed revisions,
   and immutable image digests.
2. Prove the ERP `/api/v1/sync/sub/purchase-orders` and
   `/api/v1/sync/sub/purchase-invoices` endpoints and the Selfcare
   `erp.outbox.deliver.v1` capability are enabled.
3. Prove the predecessor purchase-order and purchase-invoice endpoints are
   retired or their sender is disabled. Do not rely on a failing certificate as
   sender retirement evidence.
4. In read-only transactions, prove each target has one pinned approved quote,
   positive active lines, no `procurement_order_reference`, no PO outbox row, and
   no ERP `sub` correlation or `sub-wo:{installation_id}` PO.
5. Resolve every included vendor to exactly one active ERP supplier. Capture the
   existing Selfcare supplier-reference SHA-256, the exact endpoint-resolvable
   ERP `erpnext_id` or unique supplier code, match method, and ERP UTC
   verification time. A fuzzy or ambiguous match is a blocker. For code/name
   matches, pass the canonical ERP supplier code so the command can store it in
   both provider-reference and code fields. Do not log names, raw payloads,
   credentials, or connection strings.
6. Use at most 100 exact `installation:quote:vendor` targets. Exclude unresolved
   vendors; never guess or create a supplier as part of this command.

## Staging acceptance

Deploy the immutable candidate to the named staging Selfcare environment. Run
the operator adapter with staging-only records and a fresh command UUID:

```text
python -m scripts.procurement.cutover_purchase_orders --apply \
  --command-id <uuid> --actor <operator> --scope staging:purchase_order \
  --reason <reviewed-reason> \
  --target <installation>:<quote>:<vendor> \
  --verification '<vendor>|<current-sha256>|<erp-reference>|<verified-at>|<method>'
```

Confirm the command returns `owner=sub`, both procurement flows in `owned_flows`,
one outbox ID per target, and an audit row with action
`procurement.purchase_order_cutover`. Let the normal outbox worker deliver.
Confirm ERP acceptance, a PO identifier in the redacted response, and
`installation_projects.procurement_order_reference` write-back. Complete the
staging vendor project and confirm its approved invoice is queued, accepted by
ERP, and written to `payables_document_reference`. Re-running the same command
UUID must return `replayed=true` without another outbox row, PO, or invoice.

## Production execution

Deploy only the candidate digest accepted in staging and authorized from `main`.
Repeat all read-only preconditions immediately before execution because supplier,
quote, and ownership evidence can drift. Run one command containing only the
verified production target subset. Both procurement flows must begin with the
seeded pre-cutover owner; the command commits vendor binding changes, both
owners as `sub`, PO outbox rows, and audit evidence in one transaction or rolls
everything back.

Monitor `field_erp_sync_events` by returned event ID until every row is terminal.
For each row record status, attempts, timestamps, redacted error/response, ERP PO
ID, and the matching installation write-back. Treat `pending`, `dead`, `rejected`,
missing response IDs, or missing write-backs as unresolved incidents.

After the PO references are written back, run the existing purchase-invoice
repair owner once and verify that every eligible approved completion invoice has
exactly one `pinv-{invoice_id}` outbox row. Monitor those rows to ERP acceptance
and verify `payables_document_reference`; leave invoices without an accepted ERP
PO blocked with their recorded `payables_submission_error`.

## Failure and recovery

- Validation failure: no cutover occurred. Correct the source evidence, obtain a
  new ERP verification, and use a new command UUID if the intended inputs change.
- Transport failure after commit: do not flip ownership back or insert rows.
  Keep the stable outbox record and use its normal retry/dead-letter procedure.
- ERP accepted but write-back is absent: run the existing PO response repair; do
  not resend or manually populate the project reference.
- Unresolved supplier: leave its projects out of the batch. Finance must create
  or identify the canonical ERP supplier through the ERP-owned supplier process,
  after which a separately reviewed follow-up reconciliation command is required.
