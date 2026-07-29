# Customer VAT Exemption

## Decision

VAT exemption is an explicit, administrator-managed customer policy. It is
displayed as a checkbox beside the withholding-tax setting on person and
business customer forms. New and existing customers default to taxable.

`financial.customer_tax_policies` owns the policy record and its versioned
updates. The VAT flag is independent of withholding-tax eligibility even
though both facts share the same per-customer policy row.

## Billing behavior

Invoice authoring resolves the policy from the invoice customer at creation
time. When the customer is exempt, invoice lines are created without a tax
rate and therefore with zero VAT. Administrative drafts and recurring
subscription invoices both enforce this rule in the backend; the checkbox is
not the enforcement boundary.

Changing the policy affects invoices created afterward. Existing issued
invoices retain their recorded tax treatment and must be corrected through
the normal credit-note or replacement workflow.

The Phase 2 obligation-rating path remains a shadow comparison path until its
documented cutover. VAT exemption must be added to its authoritative input
snapshot before that path can become the invoice authority.

## Verification

- Policy defaults to disabled and can be enabled independently of WHT.
- Customer forms expose the checkbox for both customer types.
- Draft and recurring invoice construction suppress VAT for exempt customers.
- The migration backfills all existing customers as non-exempt.
