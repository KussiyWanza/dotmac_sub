# CRM sync & customer identity — UX-polish & operator-control audit

> **Status: historical audit evidence.** Revalidate unresolved recommendations against `docs/UI_INFORMATION_AND_ACTION_STANDARD.md` and the current domain SOT before implementation.

**Date:** 2026-06-29
**Method:** 2-agent parallel read-only review: (a) CRM sync/tickets/webhook/
dead-letter/duplicate-merge + the Syncs admin UI, (b) customer-identity
normalization/resolution + CRM customer upsert / portal / billing-push.
**Status:** required P0/P1/P2 remediation completed in draft PR. Part of the remaining-module audit series.

The July 20 incident read-back confirmed 436 webhook changes in the affected
window, including 169 obvious placeholder overwrites. The remaining 267 changes
are not auto-restored; they require private review for structurally suspicious
names under the interim ownership boundary where CRM is transport only and
`customer.profile_commands` owns approved legacy Subscriber name corrections
until explicit Party cutover.

> Known: full bidirectional CRM sync is deployed (incremental pull w/ watermark,
> ticket/comment push, webhooks, billing snapshots); the `crm.ticket_pull`
> feature-flag resolution bug was recently fixed (PR #506).

## Remediation status

**Last updated:** 2026-07-01
**Tracking branch:** `codex/crm-identity-ux-polish-remediation`

### Resolved in current draft

- Generic interval scheduling now skips CRM `pull_tickets` integration jobs, so
  the dedicated CRM ticket-pull beat remains the single scheduled pull path and
  UI-created interval jobs cannot collide with it.
- CRM sync detail no longer shows dead conflict/ambiguous/mapping controls for
  `pull_tickets`; the visible CRM pull settings are the ones the scheduled pull
  now reads (`page_size`, `max_pages`, and `sync_comments`).
- CRM sync run history now surfaces partial success, errors, comments, skipped
  leads, mode, and since-watermark metrics instead of masking per-ticket errors
  as ordinary success.
- The Integrations overview now renders unresolved CRM push dead letters with
  count, error, attempts, per-row re-drive, and bulk re-drive controls.
- CRM dead-letter re-drive now redirects with visible result feedback for
  dispatched rows or already-resolved/missing selections.
- CRM customer webhook updates now emit a `crm_customer_identity_update` audit
  event for existing-subscriber identity overwrites, including old/new values for
  name, email, phone, address, status, and category fields plus CRM identifiers.
- CRM customer webhook upsert logs update-vs-create match decisions and can match
  phone/name candidates using the configured phone normalizer rather than exact
  string equality.
- Identity policy now reads `default_country_code` and
  `identity_sensitive_automation_min_confidence` from registered subscriber
  settings.
- CRM billing snapshot push now reads `billing.default_currency`, respects the
  scheduler `crm_billing_push_enabled` flag, and keeps disabled runs explicit.
- CRM cache TTL, retry count, retry sleep cap, and reachability-circuit cooldown
  are registered scheduler settings and are honored by DB-scoped `CRMClient`
  instances.
- Outbound CRM subscriber webhooks route through `CRMClient` so they share retry,
  timeout, and reachability-circuit behavior.
- Reseller open-ticket counts now return/render unavailable on CRM outage rather
  than silently reporting `0`.
- The CRM sync detail Run Now action now requires browser confirmation.

### Optional follow-ups

- Log an explicit warning when `crm_billing_push` finds zero CRM-linked
  subscribers.
- Include conflicting subscriber IDs in ambiguous identity-resolution logs.
- Consider a configurable require-name toggle for CRM fuzzy matching. The current
  implementation intentionally keeps name equality mandatory while normalizing
  phone comparison.

### Verification

- `poetry run pytest tests/test_crm_webhooks.py tests/test_crm_dead_letter.py tests/test_scheduler_config_services.py -q`
  - Result: passed
- `poetry run ruff check app/services/crm_customers.py app/services/scheduler_config.py app/web/admin/integrations.py tests/test_crm_webhooks.py tests/test_crm_dead_letter.py tests/test_scheduler_config_services.py`
  - Result: passed
- `poetry run pytest tests/test_customer_identity_resolution.py tests/test_crm_billing_push.py tests/test_crm_client_resilience.py tests/test_crm_subscriber_push.py tests/test_crm_sync_handler.py tests/test_crm_webhooks.py tests/test_crm_portal_services.py tests/test_api_reseller_self_scoped.py tests/test_crm_pull_observability.py tests/test_crm_dead_letter.py tests/test_scheduler_config_services.py -q`
  - Result: passed
- `poetry run ruff check app/services/customer_identity_normalization.py app/services/customer_identity_resolution.py app/services/support.py app/services/crm_billing_push.py app/services/crm_client.py app/services/crm_webhook.py app/services/crm_customers.py app/services/crm_portal.py app/services/web_reseller_routes.py app/api/reseller.py app/services/integration_sync.py app/services/web_integration_syncs.py app/services/settings_seed.py app/services/settings_spec.py tests/test_customer_identity_resolution.py tests/test_crm_billing_push.py tests/test_crm_client_resilience.py tests/test_crm_subscriber_push.py tests/test_crm_sync_handler.py tests/test_crm_webhooks.py tests/test_crm_portal_services.py tests/test_api_reseller_self_scoped.py`
  - Result: passed

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** and
**CONTROL**. This domain is the strongest example yet of the **dead/misleading
control** signature — multiple sync-config selectors are persisted but never read.

## Acceptance criteria (CRM/identity-specific)

1. Exactly one scheduler drives each CRM pull; UI schedule and beat config can't
   double-run or collide on the run-guard.
2. Every sync-config selector shown either changes behavior or is removed/labeled
   as fixed (no dead controls).
3. Terminal sync failures (dead-letters) are visible and re-drivable from the UI.
4. Webhook-driven overwrites of customer identity are audited (old→new per field).
5. Identity/currency/country policy is read from registered settings, not env
   constants.

## Cross-cutting themes

### POLISH

**P-A. Built-but-unsurfaced + dead controls** (the signature).
- Dead-letter visibility is fully wired in the route (`crm_dead_letter_count`,
  25-row list, `/crm-dead-letters/redrive` POST) but **no template renders it** and
  there's no re-drive button — operators are blind to terminal push failures
  (`app/web/admin/integrations.py:61-62`)
- "Conflict Policy" selector (remote/local/newest/manual) persisted but **never
  read**; behavior is hardcoded remote-wins (`app/services/web_integration_syncs.py:245`, `templates/admin/integrations/syncs/detail.html:111-118`)
- "Ambiguous Match Policy" offers skip/manual_review but logic always skips;
  `mapping_primary`/`mapping_fallback` are free-text the resolver ignores (`syncs/detail.html:140-146`, `crm_ticket_pull.py:446`)

**P-B. Sync run observability.**
- Run metrics show only fetched/created/updated/skipped; stored
  `errors`/`comments_created`/`skipped_leads`/`mode`/`since` watermark are not
  shown, and a run with per-ticket errors but `fetched>0` is marked **success**
  (`syncs/detail.html:216-221`, `integration_sync.py:228-239`)
- Re-drive route ignores `redrive()`'s boolean return and always redirects with no
  success/failure flash (`app/web/admin/integrations.py:74-84`)
- CRM customer match decision (matched-via / created-new) is not logged — no
  observability into update-vs-duplicate (`crm_customers.py:145-174`)
- `reseller_open_tickets_count` returns `0` on any CRM error — indistinguishable
  from a genuine 0 → silent under-report during outages (`crm_portal.py:508-533`)

**P-C. No audit on identity overwrite (data hazard).** Every CRM webhook silently
overwrites name/email/phone/address/status with no audit row or change log — the
documented "merge two customers" hazard, with no way to see or revert
(`crm_customers.py:177-206`).

**P-D. Confirms on expensive actions.** "Run Now" (manual full sweep) and
"re-drive all" trigger immediately, no confirm (`syncs/index.html:87`). (Duplicate-
merge is CLI-only with dry-run default — no gap.)

### CONTROL

**C-1. Two schedulers can drive the same pull (config correctness).** A dedicated
beat entry runs the **incremental** pull (watermark, limit=200/max_pages=50); if an
operator sets the sync-profile job to `interval` in the UI, `list_interval_jobs`
registers a **second** beat entry running a **non-incremental full** pull. Both
write `IntegrationRun` rows and collide on the "already running" guard
(`app/services/scheduler_config.py:1173` vs `:1872`).

**C-2. Configurable-looking knobs the real run ignores.**
- The scheduled (incremental) pull uses task defaults and **never reads the job's
  `filter_config`**, so the detail-page Batch-Size / Max-Pages / Pull-Comments only
  affect manual "Run Now" (`app/tasks/crm_ticket_pull.py:12-15`, `integration_sync.py:122-124`)

**C-3. Env-only constants bypassing settings.**
- CRM cache TTL / retry policy / reachability-circuit cooldown read via `os.getenv`
  at import, not registered settings (`crm_client.py:26-36,80`)
- `crm_billing_push` currency via `os.getenv("BILLING_DEFAULT_CURRENCY")` bypassing
  the registered `billing.default_currency` (`crm_billing_push.py:54`)
- `DEFAULT_COUNTRY_CODE="234"` hardcoded for all phone canonicalization though
  `billing.default_country_code` exists (`customer_identity_normalization.py:19,30`)

**C-4. Hardcoded identity/matching policy.**
- Sensitive-automation gate hardcodes `{HIGH, MEDIUM}` acceptance — no HIGH-only
  option (`customer_identity_resolution.py:84-85,159`)
- CRM fuzzy-match uses phone exact-string equality (no normalization, unlike the
  resolution module) + mandatory name equality (`crm_customers.py:158-172`)
- `crm_billing_push` has no enable/direction control (always runs) (`crm_billing_push.py:60`)

### CONTROL/POLISH — duplicate auth path
- `crm_webhook` keeps its own JWT cache + hardcoded 10s/15s timeouts, no
  retry/circuit, unlike `CRMClient` → route pushes through `CRMClient` (`crm_webhook.py:32,76`)

## Priority

| Tier | Items |
|------|-------|
| **P0** | Completed: two-scheduler collision, webhook overwrite audit, and CRM dead-letter visibility/re-drive are resolved |
| **P1** | Completed: dead sync controls removed/fixed, scheduled-pull `filter_config` is honored, sync observability distinguishes partial success, match decisions are logged, country/currency policy reads settings, and sensitive-automation confidence is configurable |
| **P2** | Completed: CRM client cache/retry/circuit values are registered settings, CRM webhook pushes route through `CRMClient`, CRM customer phone matching normalizes, billing push has an enable flag, Run Now confirms, and reseller ticket counts show outage as unavailable |

## Appendix — full findings

### CRM sync / tickets / webhook / dead-letter
- [POLISH] (High) `app/web/admin/integrations.py:61-62` + connectors overview — dead-letter count/list + redrive POST wired but no template renders them, no button → render dead-letter panel + per-row & re-drive-all [resolved in draft]
- [CONTROL] (High) `web_integration_syncs.py:245` + `syncs/detail.html:111-118` — Conflict Policy selector persisted, never read; hardcoded remote-wins → implement in `sync_ticket` or remove + label fixed [resolved in draft]
- [CONTROL] (High) `scheduler_config.py:1173` vs `:1872` — UI `interval` job registers a second (full, non-incremental) pull alongside the dedicated incremental beat; collide on run-guard → single source / hide schedule for CRM job [resolved in draft]
- [CONTROL] (Med) `app/tasks/crm_ticket_pull.py:12-15` + `integration_sync.py:122-124` — scheduled incremental pull ignores job `filter_config`; UI knobs only affect manual run → load filter_config in `run_scheduled_pull` or document manual-only [resolved in draft]
- [CONTROL] (Med) `crm_client.py:26-36,80` — cache TTL/retry/circuit via os.getenv at import, not registered → promote to settings or document [resolved in draft]
- [CONTROL] (Med) `syncs/detail.html:140-146` + `crm_ticket_pull.py:446` — ambiguous-match `manual_review` unimplemented; mapping_primary/fallback ignored → drop or wire review queue [resolved in draft]
- [POLISH] (Med) `syncs/detail.html:216-221` + `integration_sync.py:228-239` — metrics omit errors/comments/leads/watermark; run w/ per-ticket errors marked success → show counts + watermark, flag partial [resolved in draft]
- [POLISH] (Med) `app/web/admin/integrations.py:74-84` — redrive ignores boolean return, no flash → `?redriven=N`/not-found flash [resolved in draft]
- [POLISH] (Low) `syncs/index.html:87-90`, `detail.html:27-33` — Run Now / re-drive-all no confirm → lightweight confirm [resolved in draft]
- [CONTROL] (Low) `crm_webhook.py:32,76` — duplicate JWT cache + hardcoded timeouts, no retry/circuit → route via `CRMClient` [resolved in draft]
- Verified: `crm_ticket_pull_enabled`/interval are the canonical scheduler controls; duplicate-merge CLI-only w/ dry-run + `--live`.

### Identity normalization/resolution + CRM customer/portal/billing-push
- [CONTROL] (High) `crm_billing_push.py:54` — snapshot currency via `os.getenv`, bypassing `billing.default_currency` → read from settings [resolved in draft]
- [CONTROL] (High) `customer_identity_normalization.py:19,30` — `DEFAULT_COUNTRY_CODE="234"` hardcoded for all phone canon (setting exists, unused) → make it a setting (default 234) [resolved in draft]
- [POLISH] (High) `crm_customers.py:177-206` — webhook overwrites name/email/phone/address/status with no audit/changelog (merge hazard) → emit audit event (old→new per field) [resolved in draft]
- [CONTROL] (Med) `customer_identity_resolution.py:84-85,159` — sensitive-automation gate hardcodes `{HIGH,MEDIUM}` → min-confidence setting (default MEDIUM) [resolved in draft]
- [POLISH] (Med) `crm_customers.py:145-174` — match decision (matched-via/created-new) not logged → log matched_via + field [resolved in draft]
- [CONTROL] (Med) `crm_customers.py:158-172` — fuzzy-match phone exact-string (no normalize) + mandatory name equality → reuse `normalize_phone_identifier`; make require-name toggle a setting [resolved in draft for normalization; require-name remains intentionally fixed]
- [POLISH] (Med) `crm_portal.py:508-533` — `reseller_open_tickets_count` returns 0 on CRM error (looks like genuine 0) → propagate "unavailable" [resolved in draft]
- [CONTROL] (Med) `crm_billing_push.py:60` — nightly push has no enable/disable/direction control → scheduler `crm_billing_push_enabled` flag [resolved in draft]
- [POLISH] (Low) `crm_billing_push.py:90-116` — logs nothing when 0 subscribers CRM-linked (broken link looks like no-op) → log considered/enqueued + warn on 0 [defer]
- [POLISH] (Low) `customer_identity_resolution.py:889-931` — ambiguous multi-subscriber collisions log only count, not ids → include conflicting subscriber_ids [defer]
