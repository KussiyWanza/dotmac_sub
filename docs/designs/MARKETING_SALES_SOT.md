# CRM marketing and sales owner map

**Status:** Partially verified owner map; not a campaign or retention decision
**As of:** 2026-08-17
**Decision owner:** Michael
**Current sales system of record:** Sub
**Sales authority:**
[`SALES_TO_SERVICE_LIFECYCLE_SOT.md`](SALES_TO_SERVICE_LIFECYCLE_SOT.md)
**Retirement control:**
[`CRM_WEB_RETIREMENT.md`](CRM_WEB_RETIREMENT.md) and
[`crm_web_retirement_ledger.json`](../audits/crm_web_retirement_ledger.json)

## Why this map exists

The CRM retirement ledger references this path from nine modules and 107 routes,
but the file was missing. That dangling reference made three different things
look like one unresolved owner decision:

1. the already-approved Sub sales authority for Leads, Pipelines, Stages and
   Quotes;
2. CRM campaign, survey and audience behavior that has not had a source audit;
3. unrelated contact, referral, Inbox/widget, connector and retention surfaces.

This document repairs the reference without manufacturing evidence. It maps
only authority already proved by checked-in sources. A row marked unverified
below remains unverified in the retirement ledger.

## Reconciled ownership

| CRM capability | Current authoritative owner | Verification | Migration consequence |
| --- | --- | --- | --- |
| Lead query, lifecycle state and Pipeline/Stage assignment | `sales.service`, with immutable origin owned by `sales.lead_lifecycle` | **Verified** by the approved Sales-to-Service SOT and registered contracts | Replace the CRM read/write path through the sales owner; parity, data, shadow, traffic and deletion gates remain open |
| Staff Lead creation and maintenance | `sales.lead_authoring` | **Verified** by the approved Sales-to-Service SOT and registered contract | CRM handlers must retire after caller/data cutover; they are not a second owner |
| Pipeline and opportunity-stage configuration | `sales.service`; typed settings projection through `sales.pipeline_configuration` | **Verified** by the approved Sales-to-Service SOT and boundary tests | The future reusable owner includes Pipelines and Stages; CRM SalesOrder routes in the same source module are a different slice |
| Quote query and mutable lifecycle | `sales.service` | **Verified** by the approved Sales-to-Service SOT | CRM quote CRUD is retirement input only |
| Quote authoring, line authoring and discounts | `sales.quote_authoring` | **Verified** by the approved Sales-to-Service SOT and registered contract | Preserve Sub behavior and parity tests; do not port CRM transaction boundaries |
| Quote acceptance and accepted-snapshot immutability | `sales.quote_acceptance` | **Verified** by the approved Sales-to-Service SOT and registered contract | The reusable boundary ends at acceptance; downstream order/project/service creation is excluded |
| SalesOrder reads and writes | `sales.orders` today; future ownership is outside this map | **Verified only as a separate existing Sub owner** | Excluded from `dotmac-sales`; follow the orders source-of-truth and retirement workstream |
| Campaigns, campaign steps, campaign audience, campaign sending and campaign-derived Lead creation | **No CRM-retirement owner verified by this audit** | **Unverified** | Requires a separate product-first campaign/audience/consent audit before any owner, parity or retirement state may advance |
| Surveys used as campaign or audience inputs | **No campaign owner verified by this audit** | **Unverified** | Remain in the separate campaign audit; this map does not approve a coupling to sales |
| Retention engagement history, notes, dispositions, follow-up, outreach and suppression | **Unresolved** | **Conflicting checked-in guidance** | Requires an explicit owner decision; this map neither assigns nor migrates it |
| Contacts and Party identity | `party.registry` under its own approved SOT | Outside this sales slice | Cite the Party/customer SOT; do not absorb identity into sales |
| Referrals | `referrals.program` under its own registered contract | Outside this sales slice | Keep separate from sales extraction |
| Inbox/widget/conversations and WhatsApp | Their communications owners | Outside this sales slice | No sales extraction change |
| Meta OAuth, connector transport and provider webhooks | Integrator/connector contracts | Outside this sales slice | No sales extraction change and no provider dependency in the shared module |

The CRM ledger's `sales.quote_lifecycle` label is a coarse retirement label,
not a registered Sub owner. The exact owners are `sales.service` for Quote
state/query, `sales.quote_authoring` for authoring and discounts, and
`sales.quote_acceptance` for acceptance. Likewise, `sales.lead_lifecycle` owns
immutable origin and lifecycle transitions; it does not replace
`sales.lead_authoring` or the `sales.service` query/configuration owner.

## Reusable sales boundary

The reusable sales owner stops at an **accepted Quote**. The approved extraction
target is a Starter-owned `dotmac-sales` module with one tenant-scoped authority
for Leads, Pipelines, opportunity Stages, Quote authoring, Quote lifecycle and
Quote acceptance. The boundary is an accepted, immutable commercial snapshot
plus a versioned, product-neutral owner output.

The module does **not** own or construct a Subscriber, SalesOrder, Project,
ProjectTask, InstallationProject, WorkOrder, invoice, service order or
Subscription. It does not import `dotmac-orders` or any product assembly.
Downstream owners consume the accepted-quote output after the sales transaction
commits, with durable retry and an exactly-once receipt.

This is the target boundary, not a claim that cutover has happened. Until the
module is implemented, adopted, backfilled, shadow-compared and reconciled,
Sub's current services and tables remain authoritative and the existing atomic
acceptance workflow remains the as-built behavior.

## CRM retirement slice

The missing reference covers 107 routes across eleven CRM web files. Only the
following 37 routes are part of the sales-authority slice:

| CRM web source | Routes | Treatment |
| --- | ---: | --- |
| `app/web/admin/crm_leads.py` | 8 | Sales owner mapping verified; all parity and retirement gates remain |
| `app/web/admin/crm_quotes.py` | 13 | Sales owner mapping verified; split query/authoring/acceptance at the exact owners |
| `app/web/admin/crm_sales.py` | 16 | Ten Pipeline/Stage routes are in this slice; six SalesOrder routes stay with the orders workstream |

The remaining referenced routes are not silently reclassified as sales:
campaigns (23), surveys (14), contacts (11), billing-risk retention projection
(6), Inbox widget (6), Meta OAuth (3), referrals (3), and four admin mounting
routes. Their existing ledger state remains evidence of incomplete work, not a
permission to implement or retire them.

## Cutover gates

Sales CRM writers and routes remain live until all of the following are durable
and checked in:

1. the Starter product-first dossier and exact source revisions;
2. the accepted P11 lineage evidence required by Starter ADR-0017;
3. module tenant isolation, acceptance immutability and owner-output canaries;
4. Sub backfill plus report-only reconciliation;
5. shadow comparison of counts, identities, state, money and immutable lines;
6. caller-by-caller write flip, including API, portal and import callers;
7. verified fallback removal and the retirement ledger's production
   zero-traffic window; and
8. deletion of the corresponding CRM route and writer.

Campaign, retention, Inbox, connector and downstream-order evidence cannot be
used to satisfy any of these sales gates, and the sales ruling cannot be used
to advance those other domains.
