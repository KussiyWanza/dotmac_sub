# Splynx retirement decision and cutover

Status: corrected partial retirement, 2026-07-19. The runtime import/archive
paths, dashboard sync context, and billing-ledger reconciliation view remain
retired. The database cutover now removes only the three tables proven empty
and preserves the three populated evidence tables pending a separate retention
decision.

`splynx_archived_tickets`, `splynx_archived_ticket_messages`,
`splynx_id_mappings`, `splynx_billing_transactions`, and
`Subscriber.splynx_customer_id` are deliberately **preserved**.

## Authority change

Splynx was the pre-migration BSS. Sub now owns subscribers, billing, and
support tickets natively — Splynx has had no write path into this system since
the migration completed, and the `app.services.migrations`,
`app.services.splynx_customer_sync`, and `app.tasks.splynx_sync` runtimes were
already removed.

What remains includes both empty import residue and populated historical
evidence. The runtime models and writers stay retired, but physical database
retention follows the live evidence rather than the removed code surface.

Revision 330 removes only the empty archived quote tables and the empty
portal-onboarding step counter.

### Corrected production evidence

The original retirement decision incorrectly treated `pg_stat_user_tables`
statistics as proof that the import had never run. Those statistics are not a
retention authority: they can be reset and are contradicted by current row
counts and the checked-in July 12 billing-audit evidence.

Read-only production aggregates on 2026-07-19 found:

| Table | Rows | Decision |
|---|---:|---|
| `splynx_archived_ticket_messages` | 66,314 | preserve historical support evidence |
| `splynx_archived_tickets` | 14,229 | preserve historical support evidence |
| `splynx_id_mappings` | 240,027 | preserve migration provenance pending review |
| `splynx_archived_quote_items` | 0 | retire |
| `splynx_archived_quotes` | 0 | retire |
| `portal_onboarding_states` | 0 | retire |

The ticket counts also match
`docs/audits/BILLING_ALIGNMENT_RUN_2026-07-12.md`, which already documented
their presence. The earlier zero-row claim is withdrawn.

## Preserved evidence

The populated ticket archive and ID mapping tables are retained as database
evidence, even though their runtime models remain retired. Retention does not
restore Splynx as an authority or write path. It prevents a code-cleanup change
from silently becoming a historical-data deletion.

Retained customer deletion evidence has one compatibility policy owned by
`customer.account_visibility`. An explicit `splynx_deleted=true` classifies the
legacy import as deleted, while an explicit `splynx_deleted=false` keeps the
canonical customer discoverable even when its current lifecycle is legitimately
canceled or inactive. Only an absent or unrecognized flag may use the legacy
canceled/inactive plus historical-status inference. `splynx_status` remains
historical migration evidence and never overrides the current lifecycle owned by
Sub.

The three approved retirement targets are re-counted at deploy time. Any
non-empty target fails loudly instead of losing data.

## The `splynx_customer_id` carve-out — do not remove

`Subscriber.splynx_customer_id` **stays**, and is deliberately excluded from
this retirement. It is populated on **15,263 of 15,291 subscribers (99.8%)**
in production and is load-bearing today:

- `crm_portal.resolve_crm_subscriber_id` resolves the CRM link through it —
  a subscriber without it cannot be resolved to CRM at all;
- `subscriber.py`, `subscriber_growth.py`, `billing_payment_receipts.py`, and
  `mrr_snapshot.py` read it.

It is **provenance**: an external reference to the system a subscriber was
imported from, exactly like `crm_work_order_id` on `work_order`
(WORK_ORDER_IDENTITY_SOT). Provenance is not identity, it is never a join key
for native decisions, and it retires **with CRM** — not here. A future cleanup
that sees the word "splynx" and reaches for this column would break CRM
linkage for 99.8% of the base;
`tests/architecture/test_splynx_retirement.py` asserts its presence
positively so that mistake fails a test rather than a filing.

## Cutover gate

Revision `330_retire_splynx_import_archive` counts only the three approved
retirement targets and raises with a per-table breakdown if any is non-empty.
The populated ticket and mapping tables are explicitly outside the drop set.

Do not bypass the gate by truncating. If a table has rows, someone imported
data this decision did not account for — re-open the decision.

`alembic/env.py` already wraps `op.drop_table` to no-op when a table is absent,
so the migration is safe to re-run and safe on a fresh `001_squashed` database
where the models (and therefore the tables) never existed.

## Fallback

`downgrade()` recreates only `splynx_archived_quotes`,
`splynx_archived_quote_items`, and `portal_onboarding_states` empty. The three
preserved tables and the `splynxentitytype` used by `splynx_id_mappings` are
never touched in either direction.

## Tier 2 — retired

### `legacy_bss.py` — unreachable, not a live write path

An earlier assessment recorded `legacy_bss` as "the backing store for a live
subscriber attribute", on the reasoning that its `before_flush` listener writes
`splynx_id_mappings` on every `Subscriber` flush. **That was wrong**, and the
correction is the reason this tier could be retired at all.

The listener registers *at import time*. **Nothing imports `legacy_bss`** — the
only reference in the tree was a comment in `app/models/subscriber.py`. Verified
by executing a full `import app.main` and inspecting SQLAlchemy's registry:
`app.services.legacy_bss` never enters `sys.modules`, and the app registers
**zero** `before_flush` listeners. The module could not run, and never did.

Removed with it: `Subscriber._legacy_bss_customer_id` (the staging `ClassVar`
its only reader used), `app/services/splynx_mapping.py`, and
`app/models/splynx_mapping.py`. The populated `splynx_id_mappings` table stays
as database evidence without a runtime model or writer.

The redundancy noted in the earlier assessment resolves here: `splynx_customer_id`
(a column, 99.8% populated) and `splynx_id_mappings` (a populated historical
mapping table) both modelled "this subscriber's id in the legacy BSS". The
column remains the only live application path; the table remains retained
evidence and is not a parallel decision system.

### `external_bss_adapter` — kept, splynx methods stripped

The adapter has a real non-Splynx purpose: `build_reference_payload` /
`sync_reference` handle generic external references and it is registered in
`adapter_registry` with its own tests. Only `register_splynx_mapping` and
`lookup_splynx_id` were Splynx-specific, and they had **no callers at all** —
not even tests. Those two methods are gone; the adapter stays.

### `web_admin_dashboard` sync tile — dead context, no tile

The earlier assessment called this "a permanently red tile". There is no tile:
`sync_status` was computed, placed in the dashboard context, and **never
rendered** — no template in the tree references it (the only `sync_status` in
`templates/` belongs to the unrelated OLT detail page). It was dead context, so
it is simply removed; there is no UI to redesign and nothing to fake healthy.

Removed with it: the orphaned `_network_monitoring_int_setting` helper and the
`dashboard_sync_healthy_age_seconds` setting (spec + seed), which configured
nothing once its only reader was gone. A knob that tunes a deleted computation
is worse than no knob. A stale `domain_settings` row may remain in existing
databases; it is inert.

### `web_billing_ledger` reconciliation view — removed

The ledger page cross-checked imported Splynx credits against native
`LedgerEntry` rows before `_LEDGER_CUTOVER`, display-only. With a table that was
never written it could only ever return an empty list, so the query and
`_splynx_credit_as_ledger_row` are gone. `_LEDGER_CUTOVER` **stays** — it also
bounds the invoice query and is not Splynx-specific.

## The `splynx_billing_transactions` carve-out — do not drop

The model and its table **stay**, and are asserted positively by
`tests/architecture/test_splynx_retirement.py`.

`scripts/one_off/billing_alignment_audit.py` and
`audit_void_mirror_double_reversals.py` read `SplynxBillingTransaction` as
money-adjudication evidence. The first documents running against a replica or
an isolated environment with "source tables loaded from retained Splynx
backups"; the second uses the Splynx mirror as **proof** that a void was already
absorbed into `subscribers.deposit` before it will soft-delete contra debit
ledger rows under `--apply`.

Empty in production is therefore **not** the same as "the data does not exist".
Dropping the table would remove the schema those retained backups load into, and
with it the ability to adjudicate pre-cutover money. It retires when that
reconciliation is confirmed closed — the view is gone, the evidence path is not.

ADR 0007 Phase 3 has one additional bounded evidence reader:
`billing.opening_balance_history`. It runs only in the isolated audit restore and
consumes `audit_splynx_final_balances`, which is generated from the retained
final Splynx customer-billing and complete active-transaction snapshot. It
requires one reconciled row for every migrated account; a complete empty
transaction set is zero, while a missing, duplicate, malformed, or mismatched
row aborts the whole artifact. A native account created after the fixed handoff
has an explicit zero history component plus canonical Sub-native facts.

This is a one-time authority migration, not a live Splynx integration. The
resolver cannot contact Splynx or write money, its complete result is sealed and
reviewed before materialization, and no runtime balance read consults its table.
After every prepaid funding candidate has an immutable opening and full-cohort
parity is proven, remove this reader and the obsolete partial-blocker workflow;
retain only the approved immutable Sub openings and the underlying historical
evidence according to the separate retention decision.

## Out of scope: the orphan tables

Production holds roughly 28 further `splynx_*` tables (`splynx_customers`,
`splynx_invoices`, `splynx_payments`, `splynx_routers`, …) that have **no
models**. They were created by the original import tooling, not by Alembic, and
`001_squashed` — which builds from models — never creates them. No code
references them.

They are not dropped here. Dropping tables the ORM never knew is a different
risk class from retiring models, they may be the very tables the retained-backup
workflow restores into, and it deserves its own decision rather than riding
along with a code retirement. The three newly preserved tables follow the same
evidence-retention boundary until a reviewed disposition names their owner and
proves deletion safe.

## Enforcement

`tests/architecture/test_splynx_retirement.py` keeps the removed model and
service paths absent, keeps their imports and symbols out of `app/` and
`scripts/`, and keeps the retired runtime models out of the registry.
`tests/test_splynx_retirement_migration.py` independently fixes the physical
database boundary: the three populated evidence tables must remain outside the
drop set, while only the three empty targets may be retired. The existing
`splynx_customer_id` and `splynx_billing_transactions` carve-outs remain
positively asserted.

The import-linter contract "Application code must not import retired Splynx
migration runtime" (`pyproject.toml`) forbids `app.services.migrations`,
`app.services.splynx_customer_sync`, and `app.tasks.splynx_sync`. All three were
already gone before this retirement, so the contract guards nothing today. It is
kept deliberately: it costs nothing and it stops them returning.
