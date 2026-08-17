# Invoice Due-Date Basis

## Decision

`financial.invoices` owns the issued invoice due date and the evidence that
made that date lawful. A native invoice can leave draft only through
`InvoiceIssuanceInput`, which binds these immutable values:

- `issued_at`;
- `due_at`;
- `due_date_basis`;
- `due_date_basis_ref`;
- `due_date_policy_version`;
- transition reason.

`DueDateBasis` is the Sub-local form of the Starter target contract. Its values
are `contract_terms`, `prepaid_service_period`, `provider_observation`,
`approved_manual_override`, and `unknown_unverified`.

## Invariants

A verified basis requires an aware issue time, an aware due time not before
issue, a non-empty source reference, and a non-empty policy version. Native
issuance rejects `unknown_unverified`. Imported historical observations may use
it because preserving uncertainty is more truthful than inventing a contract
or operator decision.

An explicitly unknown due date remains visible on the invoice and in finance
review stock, but cannot:

- transition the invoice to overdue;
- enter due/overdue receivable selection;
- start or advance dunning;
- suspend or otherwise restrict service.

Returning an unfunded prepaid invoice to draft clears the complete issuance
snapshot. Void and other terminal transitions retain it; terminal state does
not rewrite what was issued.

## Migration and repair

Revision `538_invoice_due_date_basis` adds nullable provenance fields. NULL is
only a legacy migration state; the application cannot create a new issued
native invoice with NULL provenance. The known pre-analysis 22 August 2026
cohort is classified `unknown_unverified` without guessing why the shared date
exists.

Billing health separately publishes open `unknown_unverified` and legacy NULL
counts. The contract gate is:

1. classify every open legacy NULL row from real source evidence or mark it
   `unknown_unverified`;
2. keep every unknown row quarantined from Collections;
3. reach zero open legacy NULL rows;
4. validate and then make provenance non-null for issued native documents.

If account management later proves the 22 August date was a commercial
decision, a reviewed owner command may supersede the unknown classification
with the exact contract/override evidence. A bulk data edit is not that command.

## Verification

- `tests/services/billing/test_invoice_lifecycle_owner.py`
- `tests/architecture/test_invoice_due_date_basis_boundary.py`
- `tests/integration/test_money_path_invariant_constraints.py`
