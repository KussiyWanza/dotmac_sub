# Party Customer Lifecycle and Attribution

**Status:** Approved; end-to-end native lifecycle implemented through revision 457
**Decision owner:** Michael
**System of record:** Sub
**Schema revision:** 457

## Decision

Sub owns the complete customer lifecycle. CRM has no runtime role, and
`dotmac_mkt` is not a lead, customer, attribution, or lifecycle authority.

The lifecycle is connected through stable identity rather than collapsed into
one overloaded status:

```text
interaction observation
        |
        v
reviewed Party identity --> Lead + immutable origin
                              |
                              v
                    manually authored Quote(s)
                              |
                              v
                     accepted Quote boundary
                              |
                              v
             exact Subscriber account conversion
                     + Lead Won + Sales Order
                         /         \
                        v           v
       Project / template / tasks   pending Subscription
                        \           /
                         v         v
                    Service Order / provision
                              |
                              v
                     active Subscription
                              |
                              v
                    Customer Experience handoff
                              |
                              v
                    Ticket / support history
```

This is not a one-way global state machine. A customer can have new Leads,
active Subscriptions, and open Tickets at the same time. Party owns who the
person or organization is; each named domain owner keeps its own lifecycle.

## Ownership map

| Fact or decision | Canonical owner | Boundary |
| --- | --- | --- |
| Person/organization identity | `party.registry` | One Party; no identity inference from one contact value |
| Inbox/social interaction | `communications.team_inbox` | Observed conversation and routing, not a sales decision |
| Native outbound campaign | `communications.campaigns` | Sub campaign, audience, recipients, and delivery state |
| Lead identity and origin | `sales.lead_lifecycle` | Party-first Lead, immutable origin, reviewed account attachment |
| Admin Person and Lead authoring and maintenance | `sales.lead_authoring` | Atomic Person Party, contact points, Organization relationship, Lead, origin, audit, and event; later profile/contact and editable Lead maintenance preserves the exact Party |
| Referral program | `referrals.program` | Capture policy, canonical program and account-attachment records, qualification/reward policy, and atomic transitions |
| Referral account orchestration | `referrals.account_conversion` | Exact Referral/Party/Lead context into atomic account creation or reviewed attachment |
| Pipeline and Quote | `sales.service` | Opportunity progress and Lead-backed commercial offer |
| Quote authoring | `sales.quote_authoring` | Atomic Lead-backed Draft/Sent Quote, lines, audit, and outbox staging |
| Accepted-Quote conversion | `sales.quote_acceptance` | Sole sales conversion event; atomic account, order, Project, Tasks, WorkOrders, audit, and outbox staging |
| Sales Order | `sales.orders` | Accepted/manual order and fulfilment handoff |
| Sales implementation coordination | `sales.fulfillment` | Structural Project/InstallationProject creation and verified release coordination |
| Service Order lifecycle | `operations.service_order_lifecycle` | Implementation gate, provisioning outcome, and activation consequence |
| Customer Experience handoff | `customer.experience_handoff` | Readiness, attention, acceptance, and durable actor/time/event evidence |
| Subscriber account | `customer.accounts` | Billing and service account owned by a Party |
| Customer credential enrollment | `auth.customer_credential_enrollment` | Purpose-bound local credential creation and Subscriber-email verification; no Party activation |
| Subscription state | `access.subscription_lifecycle` and catalog/subscription owners | Service lifecycle and access projection |
| Ticket lifecycle | `support.ticket_lifecycle` | Support case and official support history |
| Lifecycle convergence report | `customer.lifecycle_audit` | Aggregate read-only debt and coverage report |

## Party-first Lead contract

Before revision 355, every Lead required a Subscriber. That forced an account
row to exist before there was necessarily a customer or sale and made test,
lead, and account populations difficult to distinguish.

Revision 355 makes `Lead.party_id` the reviewed identity link and makes
`subscriber_id` optional account context. New Party-first Lead creation:

1. requires an active or quarantined reviewed Party;
2. records binding time, source, and reason;
3. does not create a Subscriber, role, contact point, or permission;
4. deduplicates open Leads by Party and pipeline; and
5. may attach a Subscriber later only through an approved conversion owner
   when its reviewed Party matches. In the sales workflow that owner is
   `sales.quote_acceptance`.

An exact account-attachment retry is idempotent. A different account or Party
is refused until a reviewed merge/repoint workflow exists. Legacy
Subscriber-only Leads remain readable and auditable; revision 355 does not
pretend they have already been classified.

Admin-authored Leads may also carry an optional external `reseller_id`. The
identifier is ownership staging evidence, not reseller access to the Lead and
not an identity inference. Absence means the account conversion uses the House
reseller; presence must resolve to an active non-House Reseller. Quote
acceptance copies that exact value into the new Subscriber, after which reseller
views continue to derive Quotes, Projects, and WorkOrders through
`Subscriber.reseller_id`. Email remains non-unique contact information and
never selects a reseller, merges Parties, or establishes account ownership.

The admin Edit Lead command keeps the existing Party identity fixed. It may
update that Person's explicit profile and complete default-scope contact set,
but removed contact values are retained as inactive evidence and unchanged
values retain their verification and consent state. The same transaction
updates the editable Lead opportunity values and stages audit and a PII-free
event. It cannot rewrite immutable origin, repoint the Party, or change
reseller ownership after Subscriber conversion.

## Origin and attribution contract

`lead_origin_captures` records immutable, structured evidence at Lead creation.
It deliberately separates three concepts:

1. **Native Sub campaign response:** `campaign_id` and
   `campaign_recipient_id` reference Sub's communication campaign and recipient.
   The recipient Subscriber must identify the same Party as the Lead.
2. **External advertising origin:** Meta/Google campaign, ad-set, ad, form, and
   click identifiers remain provider strings. They are not coerced into Sub
   campaign UUIDs.
3. **Direct/referral/agent origin:** controlled capture method and platform,
   optional UTM fields, a path without query parameters, and explicit capture
   evidence.

Raw webhook payloads, names, emails, phone numbers, and other contact PII do not
belong in the origin row. Provider signature receipts and protected raw event
retention, if required, belong to a separate integration/security contract.

The compatibility fields `Lead.lead_source`, `Lead.campaign_id`, and
`Lead.campaign_recipient_id` are projections of the immutable capture for new
writes. External provider IDs never populate the native campaign UUID fields.
Once captured, the origin and its lead-source projection cannot be edited by a
generic Lead update.

Approved capture methods are:

- ad lead-form webhook;
- landing page;
- portal;
- agent declaration;
- native campaign response;
- referral; and
- reviewed legacy import.

Every adapter calls `sales.capture`, which delegates immutable attribution to
`sales.lead_lifecycle`; it does not write Party, Lead, or attribution columns
independently. Revision 383 provides a signed provider-neutral webhook
capability and an authenticated staff capture command. Provider-specific raw
payload mapping remains connector configuration/code at the edge and submits
the canonical capture contract; it is not embedded in the lifecycle owner.

Revision 356 activates the referral adapter contract. Referral capture creates
a quarantined Party and unverified Party contact points, then delegates Lead
creation and immutable origin to `sales.lead_lifecycle`; it does not create a
Subscriber or store capture PII in Referral metadata. Subscriber conversion
requires exact reviewed Party equality. Activation may reconcile that exact
Party link but cannot fall back to email, phone, name, or metadata. The full
contract is `docs/PARTY_FIRST_REFERRAL_CAPTURE.md`.

Referral signup and operator adjudication carry the exact PII-free
`Referral.id + referred_party_id + referred_lead_id` context through
`referrals.account_conversion`. The coordinator locks and revalidates that
triple, asks `customer.accounts` to prepare a new account when needed, then
delegates exact Party, Lead, and Referral links to their existing owners before
one commit. It never selects identity by contact value. The full boundary is
`docs/REFERRAL_ACCOUNT_CONVERSION.md`.

For the unauthenticated handoff, capture returns a signed capability containing
only that UUID context and bounded purpose/version/time claims.
`auth.token_signing` owns the cryptographic envelope; the referral conversion
owner owns claim meaning and canonical revalidation, and resolves lifetime only
through `subscriber.referral_signup_context_expiry_minutes`. Public signup
cannot set account lifecycle, reseller, billing, verification, numbering,
permission, or marketing-consent state and never compares submitted contact
values with capture observations.

The subsequent credential handoff is separately owned by
`auth.customer_credential_enrollment`. No local credential exists until the
emailed capability, whose lifetime resolves through the auth settings owner, is
redeemed with a customer-chosen password.
Completion verifies the Subscriber account email, not the quarantined Party or
its contact point, and does not change billing-block or subscription state.

## `dotmac_mkt` boundary

`dotmac_mkt` may publish social content, manage ad-platform objects, and report
provider metrics. Those are marketing transport and provider observations.
Its Post, Channel, and AdCampaign models do not move into Sub as customer
lifecycle models.

If `dotmac_mkt` later supplies provider metadata to a Lead-capture adapter, the
adapter must submit structured origin evidence to `sales.lead_lifecycle` at
Lead creation. Sub remains the decision owner. A provider conversion count is
not a person-level conversion fact; person-level attribution is derived in Sub
from the captured Lead through Quote, Sales Order, Subscriber, Subscription,
and Ticket links.

## Downstream alignment

Revision 457 completes the Lead-first commercial boundary without duplicating
Party onto every downstream table:

- Every new Quote requires a Lead and selected Project Type and may exist
  without a Subscriber until it is accepted. A Lead may have multiple Quotes.
- A Quote carrying legacy Subscriber context must match the Lead Party. A
  legacy unbound Lead must use its exact legacy Subscriber.
- A Sales Order linked to a Quote must use the Quote's exact Subscriber.
- A Ticket may be Lead-only, which supports pre-sales questions. If it also
  links Subscriber/customer account/person rows, every linked Subscriber must
  match the Lead Party; a legacy Lead requires its exact Subscriber.
- `Subscriber.sales_order_id`, Subscription, and downstream support links stay
  with their current owners and are measured for convergence by the audit.

Draft/Sent Quote authoring creates no Subscriber or downstream fulfillment
records. Transitioning a Lead-backed Quote to Accepted is the only sales event
that creates or attaches the reviewed account, marks the Lead Won, creates the
SalesOrder and copied lines, copies the Quote Project Type to the Project,
assigns the active template configured for that type, and creates its Tasks.
WorkOrders are automatic only where a template-task automation policy enables
them; users may otherwise create them from the Project or a Project Task. Those
state changes plus audit and durable event records commit in one transaction
and replay by Quote identity without duplicates.

When the Lead carries reviewed external reseller ownership, the same atomic
conversion creates the Subscriber under that reseller before the SalesOrder and
implementation scope are staged. SalesOrder, Project, Task, and WorkOrder rows
retain their structural Subscriber links and do not duplicate reseller state.

## Subscription and billing block independence

Billing enforcement work does not conflict with this slice. Party and Lead
links answer identity and acquisition questions. Subscription status and
access restriction answer service and network-access questions.

A billing block may project a Subscription/account into blocked access without:

- changing Party identity;
- rewriting Lead, origin, Quote, or Sales Order;
- changing attribution;
- removing the customer from historical lifecycle cohorts; or
- preventing support history from remaining linked.

The lifecycle audit reports Subscription counts by the canonical status
vocabulary, including `blocked`; it does not decide or change that status.

## Legacy debt and audit

Revision 355 is additive and performs no backfill. Campaign compatibility and
Ticket-to-Lead foreign keys are installed as PostgreSQL `NOT VALID` constraints
so new writes are protected without falsely claiming historical rows are clean.
They are validated only after the audit and reviewed repair work reach zero
unresolved violations.

The signed CRM `customer.accepted` endpoint is now observation-only through
`integration.inbox`. It cannot create a Subscriber or write any existing
Subscriber name, email, phone, address, category, date of birth, gender,
status, Party binding, or lifecycle state. Exact retained `crm_person_id`,
`crm_sales_order_id`, or `crm_quote_id` provenance may report a read-only match;
name and contact observations never establish identity. Unmatched and ambiguous
observations remain in the Inbox for review instead of becoming accounts.
The incident command at
`scripts.one_off.restore_crm_placeholder_identity` is read-only by default.
Its apply mode requires the exact digest from a fresh plan, an attributable
actor and reason, and an explicitly named target. It locks and revalidates the
complete candidate set, delegates legacy Subscriber corrections to
`customer.name_repairs`, refuses Party-bound rows, and commits identity-index,
per-account audit, batch audit, and `subscriber.updated` evidence atomically.

Run the PII-free report in a read-only, repeatable-read transaction:

```bash
python -m scripts.migration.audit_customer_lifecycle
```

The report contains aggregate counts only for:

- Party/Subscriber Lead binding coverage and mismatch debt;
- structured origin coverage and projection drift;
- Party-first referral capture, quarantined adjudication readiness, conversion,
  and legacy PII-metadata debt;
- native campaign/recipient validity;
- Quote-to-Lead and Order-to-Quote alignment;
- Subscriber-to-Sales-Order and Subscription coverage; and
- Ticket-to-Lead/account alignment.
- SalesOrder-to-Project/InstallationProject/ServiceOrder convergence and CX
  handoff coverage.

It never binds a Party/account, infers attribution, changes a lifecycle state,
or prints identity values.

## Cutover gates

1. Deploy revisions 345 and 346 without data writes.
2. Run and retain the aggregate lifecycle audit.
3. Classify legacy Subscriber-only Leads and invalid campaign/Ticket references
   through a protected reviewed worklist.
4. Enable capture adapters one at a time and verify exact-retry/idempotency,
   Party match, provider signature, and PII-minimization behavior.
5. Shadow lifecycle/attribution reporting against the captured join chain.
6. Repair legacy debt through separately approved commands; do not infer it in
   the migration.
7. Validate deferred foreign keys only after zero unresolved violations.
8. Retire metadata-only attribution inference and any CRM/mkt bridge fallback
   after parity is proven.

Rollback before reader cutover disables new capture adapters and command use;
it does not delete captured evidence. No production migration, backfill,
adapter cutover, or deferred-constraint validation is authorized by this
document or revisions 345/346.
