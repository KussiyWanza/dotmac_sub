# Dotmac CRM Web Capability Retirement

## Goal

Build every operational capability exposed by every `dotmac_crm` web module in
Sub, migrate the required data, callers, traffic, and jobs, prove parity and
cutover, and then fully decommission `dotmac_crm`.

This is a capability and usable-surface migration. It is not a literal copy of
CRM handlers, templates, transaction boundaries, or legacy ownership. Every
replacement must use Sub's named source-of-truth owner. A capability that does
not belong in Sub still needs an explicit replacement, redirect, or reviewed
removal with caller and traffic evidence.

## Controlled Baseline

The machine-readable migration control is
`docs/audits/crm_web_retirement_ledger.json`. Its source baseline is CRM
revision `87f6273d040a3c3cc27213801da80ee91d278673`.
The current Sub assessment baseline is revision
`680a9ca2a9ec1ca2097a6841f47700df1a114539`, after reviewing merged PRs
#1601 through #1617, #1619, #1621, and #1623 through #1625. Updating the Sub
target revision requires a fresh capability review; advancing a Git reference
alone is not evidence.

| Initial triage class | Modules | Routes | Meaning |
| --- | ---: | ---: | --- |
| Covered candidate | 17 | 89 | Sub appears to have the capability; parity and retirement remain unproved |
| Usable-surface gap | 11 | 95 | Relevant Sub backend exists, but the needed operator or public surface is absent or unverified |
| Partial capability | 29 | 541 | Related behavior exists, but the full capability, surface, data, or operational closure does not |
| Owner/policy gap | 7 | 64 | The authoritative owner or policy must be decided and built |
| Replacement/retirement | 9 | 24 | The CRM path should be replaced, redirected, or explicitly removed rather than copied |
| **Total** | **73** | **813** | Every module and route is in scope |

These classes prioritize discovery; none is a completion state. In particular,
“covered candidate” does not mean done, and “replacement/retirement” does not
mean optional.

The inventory includes:

- all route-bearing modules and helper/builder modules under `app/web`;
- every GET, POST, and DELETE decorator;
- effective paths after nested `APIRouter.include_router()` prefixes, including
  `/admin` and `/admin/crm`;
- handler, source line, route-local path, template references, and the module's
  CRM service/model dependencies;
- sparse per-module and per-route migration tracking backed by explicit
  top-level defaults.

## Completion Gate

A route may move to `retired` only when the ledger records evidence for all of
the following:

1. production usage is classified;
2. the replacement capability, registered owner, and usable surface—or an
   explicit redirect/removal—are named;
3. behavior, permissions, audit, events, idempotency, and error behavior are
   verified or reviewed as not applicable;
4. data and caller migration are verified;
5. shadow comparison is verified where applicable;
6. cutover and rollback are documented and verified;
7. fallback paths are removed;
8. the checked-in zero-traffic observation contract shows zero CRM traffic; and
9. the CRM route is deleted.

A module may move to `retired` only after its owner decision is verified, all
its routes have passed the route gate, its fallbacks are removed, its zero-
traffic window has passed, and the CRM source module is deleted. Helper modules
with no routes still pass the module gate.

Evidence must name durable repository paths, tests, migration/reconciliation
reports, dashboards or queries, and approved operator records. A statement that
Sub has a similarly named route is not parity evidence.

### Sales owner-map correction

The previously missing
[`MARKETING_SALES_SOT.md`](MARKETING_SALES_SOT.md) now reconciles the ledger's
mixed marketing/sales references. The approved
[`SALES_TO_SERVICE_LIFECYCLE_SOT.md`](SALES_TO_SERVICE_LIFECYCLE_SOT.md)
verifies Sub's present owners for Leads, Pipelines, Stages and Quotes through
acceptance. That owner decision does not prove behavior parity, migrated data,
shadow agreement, cutover, zero traffic or deletion, so the corresponding CRM
routes remain unretired.

The same correction explicitly does **not** verify campaign, survey/audience or
retention-case ownership. Those rows require separate audits and must not
advance by association with sales. CRM SalesOrder paths likewise stay outside
the sales slice and follow their separate orders authority.

### CRM reporting ownership decisions

Self-Care hosts operational report projections over facts it already owns. The
current capability/route contract is
`docs/designs/CRM_REPORT_CAPABILITY_MATRIX.md` and the registered read owner is
`ui.crm_operational_reports`. CRM report routes remain in shadow verification
until the normal route retirement gate above is complete.

Two legacy surfaces are explicitly outside this migration:

- the orphaned manually populated Quarterly Report and its local XLSX inputs
  are not recreated in Self-Care; the supported NCC complaints workbook is a
  separate native capability;
- raw customer-retention engagement history, agent notes, dispositions,
  follow-up dates, pipeline state, campaign/outreach history, contact
  preferences, reminders, and suppression remain CRM-owned. Self-Care neither
  copies those records nor creates a competing retention state machine.

The legacy CRM **scheduled NCC complaints email is not part of the orphaned
Quarterly Report exclusion**. Its replacement is
`communications.ncc_weekly_delivery`: a configurable Tuesday XLSX delivery
with durable occurrence and artifact evidence. It remains disabled until the
comparison, staging acceptance, CRM-job disablement, and rollback gates in
`docs/runbooks/NCC_WEEKLY_REPORT_CUTOVER.md` are complete.

Where an aggregate needs a fact with no authoritative owner—currently downtime
credit decisions and project-task actual effort—the report renders the value as
unavailable and names the ownership gap. It must not estimate or fabricate it.

### Zero-traffic evidence

The zero-traffic gate uses Dotmac Observability, not an operator's memory or a
point-in-time screenshot. The default window is 30 consecutive days beginning
only after the CRM route has been cut over and its fallback has been disabled.
A longer window may be required by the owning design; a shorter window does not
satisfy this gate.

The primary evidence source is the production `request_completed` stream in
Loki, selected with `{app="dotmac-crm",environment="production"}` and filtered
by the ledger route's exact method and effective route-template path. The
corroborating source is the CRM application's `http_requests_total` counter in
VictoriaMetrics, queried with the production CRM target labels plus the same
method and path over the same window. The precise query templates and required
evidence fields are versioned in
`docs/audits/crm_web_retirement_ledger.json` under
`zero_traffic_evidence_contract`.

Both ingestion paths must have health evidence covering the complete window.
Missing, stale, reset-without-history, or otherwise blind telemetry is not
evidence of zero traffic. A verified gate records the window timestamps, both
queries and zero results, telemetry-health references, and the approved
operator record. Module retirement aggregates the same evidence across every
route in that module.

## Delivery Sequence

Work proceeds in coherent domain slices:

1. assign the capability to an existing registered Sub owner or define the
   missing owner and policy;
2. implement typed owner commands/queries and usable web/API/mobile surfaces;
3. migrate and reconcile data and historical provenance;
4. migrate callers, links, scheduled work, webhooks, and operator workflows;
5. shadow or otherwise compare behavior and projections;
6. cut over behind a documented fail-closed gate;
7. verify the zero-traffic window and remove fallbacks; and
8. delete the corresponding CRM routes, templates, services, jobs, and finally
   the CRM deployment.

### Portal-chat authority exception — CLOSED 2026-08-30

ADR 0006 temporarily paused the portal-chat portion of CRM retirement. That
exception is closed, and not by the cutover gate it named: the CRM was
decommissioned on 2026-08-29, so the transport it assigned authority to no
longer exists.

Portal and public live chat is owned outright by
`communications.team_inbox_widget`. The `crm.chat_session.v1` capability is
removed from the `dotmac.crm` manifests since 1.2.0, the
`comms.chat_session_authority` selector and the CRM broker are deleted, and
`tests/architecture/test_single_chat_authority.py` prevents a second chat
authority returning. Retirement record: `docs/adr/0006-temporary-crm-chat-authority.md`.

This closes the exception; it does not by itself advance a ledger route to
`retired`. Final CRM removal still requires reconciliation, traffic evidence,
capability cutover, fallback retirement, and source deletion for the remaining
CRM surfaces — ticket observation, subscriber observation, portal session, and
the inbound `/api/v1/crm/*` API Sub serves.

### Sales quote transport retirement

`sales.crm_quote_retirement` owns the immutable decision that the external CRM
quote transport is retired. ERP remains a separate active integration. Native
Selfcare quote reads, authoring, and payment owners retain their existing
feature controls; this retirement neither enables nor disables native quoting.

Migration 579 disables every persisted schedule with either retired quote task
name, including differently named aliases. Scheduler reconciliation repeats
this idempotently. Both task names remain broker-compatible tombstones: they
return one typed retirement outcome without opening a session or processing
subscribers. The old service reconciliation entry points also do nothing.

Historical `QuoteMirror` and `QuoteSyncState` rows, payloads, IDs, and timestamps
are preserved. Mirror reads never fetch CRM, enqueue a refresh, or move
`synced_at`. Web reads explicitly label saved information as historical; API
mirror reads return `source_state=retired` and `actions_available=false`.
Old mobile versions may ignore those additive fields, but request, accept, and
deposit preflight always refuse through the existing domain-error boundary,
regardless of a leftover enabled binding. No new payment is taken by the
retired deposit path. Previously received payments needing manual
reconciliation remain the financial owner's responsibility; operators must not
replay CRM acceptance or alter historical rows.

Quote-specific CRM client, facade, and runner methods are removed. The current
CRM manifest is 1.3.0 without `quote_command`; all published older pins remain
immutable compatibility facts. No installation is enabled or automatically
adopted.

The shared connector remains because subscriber and ticket observations,
portal-session transport, referrals compatibility, and inbound events still
have callers. Their replacement or retirement is a separate slice. The quote
webhook admission and historical upsert helper remain because inbound history
and native quote deposit synchronization still reference them. This slice sends
no notifications and enables no inbound binding. ERP work-order mappings retain
legacy CRM project and ticket identifiers when native references are absent.

Customers needing a new quote while native quoting is disabled are directed to
support; no replacement data source or pricing decision is invented.

Focused validation covers queued tasks, absent/disabled/enabled bindings,
refusal audit, history and timestamp preservation, scheduler aliases, retirement
copy, and native quote behavior. Do not roll back to a worker that calls the
retired transport. Preserve migration 579 storage, its tombstones, and all
historical quote and synchronization rows during any rollback. Migration 579's
downgrade therefore moves only the Alembic revision marker; its idempotent
upgrade accepts the retained singleton and reasserts schedule retirement.

Each slice updates the ledger, the owning design and relationship-map entries,
the executable SOT registry when an owner changes, behavior tests, architecture
guards, and operator guidance together.

The first implementation priorities are the owner/policy gaps that block other
flows, followed by usable-surface gaps, then the highest-use partial modules.
The exact order is recorded through each module's `target_slice`; no class is
excluded from the terminal goal.

### First blocking slice: service-team lifecycle

The first selected slice is CRM's 11-route
`app/web/admin/service_teams.py` capability. This is upstream of ticket-to-work-
order issuance, Inbox routing, workqueue scope, outage coordination, project
assignment, and field execution.

The native identity foundation is retained, but scalar type, region, manager,
member role, and workforce-department fields are only migration shadows.
`operations.service_team_lifecycle` owns stable team identity and Party-backed
membership. `operations.service_team_composition` separately owns registered
capabilities, many member responsibilities, typed geographic scope, explicit
team relationships, provider-neutral external observations, and exact routing
policies.

The CRM-retirement slice therefore:

1. retires `support_service_teams` and `support_service_team_members` without
   importing CRM memberships or making identity matches;
2. verifies the five retained native team pointers before source retirement;
3. removes only compatibility memberships that would block migration 426;
4. keeps RBAC permission grants separate from operational responsibility;
5. moves each consumer to set-valued capability/scope queries or an explicit
   assignment/routing policy;
6. preserves historical references through soft lifecycle; and
7. drops scalar shadows only in a later forward migration after complete-cohort
   shadow verification and rollback expiry.

The detailed contract and operator procedure are
`docs/designs/SERVICE_TEAM_LIFECYCLE_SOT.md` and
`docs/runbooks/SERVICE_TEAM_PARTY_CUTOVER.md`.

The executable registry records native lifecycle authority as complete and
composable authority as shadowing. Production migration evidence,
complete-cohort drift verification, rollback expiry, and the later scalar
contract migration remain open gates.

This is not an identity-system replacement. `party.registry` remains the
canonical Person Party owner and `auth.staff_provisioning` remains the staff
principal owner. Service-team lifecycle consumes those identities and owns only
stable team identity, Party-backed membership, activation, and retirement
facts.

### Reconciliation after the Inbox and project PRs

The reviewed Sub target contains material capability work that the original
triage predated:

- PRs #1602 through #1611 completed substantial Inbox workspace, routing,
  collaboration, attachment, initiation, snooze, scheduled-send, transcript,
  ticket-handoff, and operator workflow behavior; PR #1615 added the field-job
  conversation lifecycle. The corresponding CRM Inbox
  modules are now `implementation_in_progress`, not complete: route-specific
  owner mapping, history migration, channel configuration and traffic cutover,
  authenticated browser parity, fallback removal, zero traffic, and CRM source
  deletion remain.
- PR #1610 completed the native project-task to work-order operator workflow,
  and PR #1617 added the vendor-delivery projection. CRM project administration
  remains `implementation_in_progress` until data, callers, traffic, and source
  deletion pass the retirement gates.
- The agent workqueue is the next native slice. `operations.agent_workqueue`
  owns native service-team scope consumption, ranking, personal snooze state,
  and action coordination. `/admin/workqueue` replaces the seven CRM page,
  partial, snooze, claim, and complete behaviors. Ticket and Team Inbox
  lifecycle owners participate in the coordinator transaction; Work Orders
  remain open/snooze-only because dispatch and field owners retain their
  transitions. See `docs/designs/AGENT_WORKQUEUE_SOT.md`.

No local CRM-retirement worktree or unpushed branch is accepted as target
evidence. Only behavior present at the recorded Sub revision may advance a
module's assessment.

## Refresh and Validation

Refresh from a clean checkout or archive of the pinned CRM revision:

```bash
poetry run python scripts/architecture/crm_web_retirement.py refresh \
  --crm-root ../dotmac_crm \
  --source-revision 87f6273d040a3c3cc27213801da80ee91d278673
```

The refresh preserves reviewed tracking data for unchanged stable route IDs.
Review every added, removed, or changed route before accepting the new source
revision.

Validate the checked-in control in CI:

```bash
poetry run python scripts/architecture/crm_web_retirement.py validate
poetry run pytest tests/architecture/test_crm_web_retirement.py -q
```

For a cross-repository drift check, add `--crm-root` and
`--source-revision` to `validate`. CI does not need access to a sibling CRM
checkout to enforce the checked-in completeness and retirement gates.

## The vocabulary freeze, and the distinction that prevents data loss

The CRM was decommissioned 2026-08-29. What remains inside Sub is replaced
**domain by domain** — Inbox/Chat, then Support/Ticketing, Sales/Quotes,
Party/Customer/Reseller, the relevant ERP modules, and finally a
secrets/deployment/observability pass — each slice deleting residue **beside
its replacement owner** rather than in a sweep. Michael's rule for every slice:

> it cannot claim cutover while its displaced CRM reader, writer, flag or
> transport remains.

A slice-by-slice replacement only works if the surface holds still in between,
so the surface is frozen: `tests/architecture/test_crm_vocabulary_freeze.py`
over the baseline in `tests/architecture/crm_vocabulary_baseline.txt`. It is
two-directional — a new CRM/Omni dependency cannot land, and an existing one
cannot vanish without the baseline being lowered in the same change. Deleting
CRM code therefore fails the guard until you lower the baseline, which is the
guard working rather than a defect.

### The surface is mostly not CRM-owned

Measured at `a12b9ebca`, before this change: **705 files** — 251 `app/`, 248
`tests/`, 106 `docs/`, 71 `alembic/`, 29 `scripts/`. The committed baseline
carries **708**, because the freeze's own three files name the vocabulary they
police. Of the `app/` files only about 21 are *named* for the CRM. The rest
merely reference it.

Matching is on identifier **tokens** with camelCase split first, not on the
bare word: `\b` never fires beside `_` or a digit, so a word-boundary search
reads `crm_subscriber_id`, `CRMClient` and `crm_ticket_pull` as clean — which
is nearly the whole real surface.

### CRM-owned artifact versus Sub-owned artifact with a CRM-era name

**This is the distinction that prevents data loss, and it must survive into the
Sales/Quotes and Party slices.**

Several tables were seeded as CRM mirrors and have since become Sub-owned data
that merely carries a CRM-era name. The name is cosmetic debt. The data is not.

- `quote_mirror` (`app/models/quote_mirror.py`) and `referral_mirror`
  (`app/models/referral.py`) are **real Sub-owned tables**, not caches of
  anything that still exists.
- `quotes_mirror` is read by **four live surfaces**:
  `app/web/customer/quotes.py`, `app/api/reseller.py`, `app/api/me.py` and
  `app/api/crm_webhooks.py`.
- Its writers include `app/services/quote_deposits.py` and
  `app/services/sales/selfserve.py` — **Sub's own self-serve selling flow**,
  not CRM adapters.
- `work_order_mirror` is referenced across **42** `app/` files.

Dropping any of these because the name says "mirror" or "crm" would delete live
quote and referral data. The Sales/Quotes slice retires the CRM *transport* and
the refusal-compatibility path; renaming the Sub-owned tables is separate
cosmetic work with its own expand/backfill/contract migration, and it is not a
prerequisite for that slice's cutover claim.

The general test, applied per artifact rather than per name: **does anything
that still exists write it?** If a surviving Sub owner writes it, it is
Sub-owned data with an unfortunate name. If only the departed CRM wrote it, it
is residue.
