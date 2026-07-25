# Platform Adoption Ledger — dotmac_sub (Phase 0)

**Status:** Draft for review — Phase 0 of the platform adoption program. No code or
schema changes are authorized by this document.
**Decision authority:** `dotmac_starter_mt` `docs/adr/0003-unified-deployment-profiles.md`
(one platform kernel; products are thin assemblies) and
`docs/superpowers/plans/2026-07-18-existing-product-adoption.md` (this repo's track).
**Companion source of truth in this repo:** `docs/SOT_RELATIONSHIP_MAP.md` and its
executable registry `app/services/sot_relationships.py` — the per-domain owners named
there remain authoritative; this ledger classifies each concern *against the future
kernel contracts*, it does not re-assign ownership.
**Recon basis:** repo state at `origin/main` 7807afcd, surveyed 2026-07-19.

## The adoption frame

```text
dotmac_sub = platform kernel + dedicated-one-tenant assembly + ISP domain modules
```

- The ISP **operator** (this deployment) becomes exactly one platform `Tenant`.
- **Subscribers, resellers, organizations remain product-domain records** beneath that
  tenant — never platform tenants.
- Adoption is incremental: seams and adapters first; identity/tenancy/entitlement
  replacement only after the matching kernel contract is *released* and parity tests
  pass. No big-bang rewrite, no shared database with other products.

Classifications used below:

| Class | Meaning |
|---|---|
| **reuse** | Shape already matches the kernel contract; adopt it (nearly) as-is when released |
| **adapt** | Keep the existing owner; put the kernel contract behind an adapter seam |
| **product-owned** | ISP-domain forever; the kernel never owns this |
| **migrate-later** | Known convergence target, blocked on prerequisites or a forcing event |
| **retire** | Already scheduled or newly proposed for removal |

## Ledger

### Identity, authorization, context

| Concern | Current authority | Class | Notes |
|---|---|---|---|
| Tenant/operator context | **None exists** — single-operator app; no `tenant_id` columns, no tenancy middleware | **reuse** | Greenfield for the kernel tenant-context contract; one tenant row at adoption. `Organization` (`app/models/organization.py`) is a B2B customer party, NOT tenancy |
| Request principal context | `auth` dict from `require_user_auth` (`app/services/auth_dependencies.py`) | adapt | Wrap in the kernel request-context shape; do not rewrite consumers |
| Subscriber/reseller/staff identity | `app/models/subscriber.py` (`Subscriber`, `Reseller`, `ResellerUser`), `app/models/system_user.py` | **product-owned** | These are the product parties under the one tenant. `PartyStatus`/`UserType` enums + `person_id = synonym("subscriber_id")` show a Party migration already started in-schema |
| Credentials, sessions, MFA, API keys | `app/models/auth.py` (`UserCredential` with XOR principal constraint, `Session`, `MFAMethod`, `ApiKey`); single flow owner `app/services/auth_flow.py` for all four surfaces (admin/customer/reseller web + JSON/mobile API) | adapt | Single owner already; the `person_id` synonym and XOR-principal constraint are the natural adapter seam to the kernel Party/identity contract |
| Roles/permissions | `app/models/rbac.py` + guards in `app/services/auth_dependencies.py` (`require_role`, `require_permission`, `require_scoped_permission`, `require_method_permission`); router-level declarative modes in `app/main.py` | adapt | String permission keys (`<domain>:read/:write`) map cleanly onto manifest-declared codes. **Convergence item:** a second `require_scoped_permission` implementation exists in `app/services/field/vendor_auth.py` — fold into the central guard (migrate-later) |
| Settings | `DomainSetting` (`app/models/domain_settings.py`) + `app/services/settings_spec.py` registry/resolver + seed/cache | adapt | Same settings-as-data shape the kernel standardizes; kernel contract slots behind `resolve_value` |
| Feature flags / module gates | `app/services/control_registry.py` (`is_enabled` — MODULE/FEATURE/SAFETY layers) over `module_manager.py` | adapt | Already one read path with declared fail direction. Legacy alias keys are instrumented for removal: **retire** those |
| Audit | `AuditEvent` (`app/models/audit.py`); writers `app/services/audit.py` (`AuditEvents.create/.record`) and the `record_audit_event` façade (`app/services/audit_adapter.py`) | adapt | Two sanctioned surfaces (adapter + in-transaction `stage`), zero stray callers — pinned by `tests/architecture/test_audit_writer_surfaces.py`. Kernel audit contract adopts behind `record_audit_event` |
| Session/transaction ownership | `app/db.py` (`get_db` never commits; `task_session` commits; `form_write` rollback guard) + `app/services/unit_of_work.py` | adapt | **Mixed commit ownership** (services, `auth_dependencies` API-key touch, `task_session`, `UnitOfWork(auto_commit)`). The kernel one-transaction-owner contract is the formalization target |

### Commercial lifecycles

| Concern | Current authority | Class | Notes |
|---|---|---|---|
| Catalog/offers/pricing | `app/models/catalog.py` + `app/services/catalog/*`, exposed to network via `service_intent.*` adapters | adapt | Catalog stays ISP-shaped; kernel entitlement/offer contracts sit behind the existing `service_intent` seam. Note: `catalog.py` mixes commercial and network-access models in one module |
| Subscription lifecycle | `app/services/account_lifecycle.py` (legal transitions, suspend/restore/activate/expire/cancel, `compute_account_status`) + `EnforcementLock` ledger; access decision in `access_resolution.py`; network consequence in `enforcement.py` | **reuse** | Already kernel-shaped (state machine + lock ledger + observation/decision/consequence split). Raw-writer consolidation was completed by the 2026-07-13 re-audit (see `tests/test_access_enforcement_strays.py`) and is now pinned by `tests/architecture/test_subscription_status_writers.py` |
| Billing/invoicing/payments/ledger | `app/services/billing/*` over `app/models/billing.py` (invoice-state legality, allocations, ledger, credit notes, tax) | adapt | Money is **Decimal-clean** (`Numeric(12,2)`, no float money found); single-currency NGN hardcoded on 8 tables. Kernel `Money` type = reuse; FX is a later low-friction bolt-on |
| Dunning/collections | `app/services/collections/_core.py` + policy sets; "dunning owns postpaid enforcement; prepaid enforcement owns prepaid access" | product-owned | ISP-policy machinery, not kernel material |
| Usage/metering/FUP | `app/models/usage.py` + `app/tasks/usage.py` (RADIUS accounting import, rating runs, quota, FUP) | product-owned | Kernel metering could front `UsageRatingRun`/`QuotaBucket` at earliest migrate-later. **Convergence item:** FUP *decision* logic lives inside the task body against the repo's own thin-task rule — extract to a service before any kernel job-contract adoption |

### Operations and infrastructure

| Concern | Current authority | Class | Notes |
|---|---|---|---|
| Provisioning / network operations | `NetworkOperation` (`app/models/network_operation.py`) + `app/services/network_operations.py` (tracked-operation lifecycle, `tracked_operation`/`run_tracked_action`); vendor adapters under `app/services/adapters/` and `app/services/network/` | adapt | A homegrown provider-job contract; the kernel provider-job interface becomes an adapter over it. Vendor/OLT/ONT semantics stay product-owned forever |
| Events / outbox / jobs | `EventStore` + `app/services/events/dispatcher.py` (persist-then-dispatch with retries); explicit ERP outbox `app/services/dotmac_erp/outbox.py`; task idempotency (`IdempotencyKey`), heartbeats (`TaskExecution`), Postgres advisory locks | **reuse** | Matches the kernel command/outbox/job contract shape nearly 1:1 — the ERP outbox is the template, the event store the generalization target. DB-backed beat (`DbScheduler`) stays product-owned |
| Outbound webhooks | `integration.delivery` + `webhook.http` `events.deliver.v1` binding | **reuse** | Canonical subscription, HMAC signing, delivery, retry, dead-letter, and replay owner; superseded webhook tables/tasks are removed |
| Payment gateway inbound | Paystack/Flutterwave typed capabilities + `integration.inbox` → `services/api_billing_webhooks.py` consequence adapter | **reuse** | Signature-verified receipt identity and replay are platform-owned; billing alone decides settlement and money state |
| Files | `StoredFile` + `app/services/object_storage.py` (S3-compatible) | reuse | Direct fit for the kernel storage provider interface |
| Notifications | Policy layers (`notification_channel_policy`, event policies, suppression) over transports (email/SMS/WhatsApp) | adapt | Policy stays product-owned; transports are provider-interface candidates. Rule already enforced: domain services request an outcome, never construct rows or pick channels |
| Search | DB typeahead only (`services/typeahead.py`) | product-owned | No index; no kernel contract needed |
| Observability | `app/metrics.py` (Prometheus), `app/observability.py`, optional OTel (`app/telemetry.py`), `/health`, task-reliability services | reuse | Kernel health/telemetry contract maps directly |
| Secrets | `app/services/secrets.py` (OpenBao/`bao://` URI resolver) + credential crypto/rotation | reuse | Already kernel-grade; four-tier policy documented in the SOT map |
| Migrations/deploy/CI | 307 alembic revisions (`upgrade heads`); `scripts/deploy.sh` (backup → pin → migrate → recreate → health gate → retention); CI with import-linter boundary gate | product-owned | Per-repo mechanics. Kernel adoption extends the import-linter contracts to guard new kernel seams |

### External integrations

| Concern | Current authority | Class | Notes |
|---|---|---|---|
| ERP sync | `dotmac.erp` versioned capabilities + `field_erp_sync` outbox + reconcilers | **reuse** | Transport operations use one version-pinned installation; financial and operations owners decide every consequence |
| CRM observations and mirrors | `dotmac.crm` versioned capabilities + `integration.inbox` + domain mirror reconcilers | **reuse** | The transitional native-sync dual writer and CRM-specific delivery store are removed; Sub is authoritative for support and operational state |
| Field/customer mobile APIs | `app/api/field/*`, `/api/v1` subscriber surface | product-owned | Sub is authoritative; apps are API-only clients per the app-independence standard |
| Splynx legacy | `models/splynx_*.py` | retire | No live tasks reference them; archive-table drop program already in flight |

## Multiple-writer findings (the Phase-0 actionable list)

These are the parallel decision paths the adoption plan requires a cutover-and-removal
gate for. Verified at 7807afcd:

1. **`Subscription.status` single-writer consolidation — DONE, now pinned.** At
   this cutover the only modules assigning `SubscriptionStatus` are the owner
   (`account_lifecycle`) and the snapshot-restore tool
   (`web_system_restore_tool.py`, maintenance exemption). Generic catalog
   updates reject lifecycle fields and cannot create administrative locks.
   The historical strays (CRM API, reseller portal, and catalog update — S3 of
   the 2026-07-13 re-audit) are routed through the owner. The consolidation is
   pinned by `tests/architecture/test_subscription_status_writers.py`
   (AST-based, shrink-only allowlist, sensitivity-proven).
1b. **`Subscriber.status` mutated in-memory for display** — `web_reports.py`
   (three sites) and `subscriber_growth.py` assign a *derived* `AccountStatus`
   onto live ORM `Subscriber` rows purely for report filtering/rendering. The
   request path never commits, but mutating persistent objects for presentation
   is an autoflush hazard and makes the UI a parallel projection of account
   status. Candidate cleanup: derive into a view-model field instead of the ORM
   attribute.
2. **Audit writer consolidation — DONE, now pinned.** At origin/main there are
   zero direct `AuditEvents.create/.record` callers; the two sanctioned
   surfaces are `record_audit_event` (adapter, request/consequence paths) and
   `AuditEvents.stage` (stages in the caller's transaction — the correct
   surface for commit-owning services; billing uses it deliberately). Pinned by
   `tests/architecture/test_audit_writer_surfaces.py`.
3. **Scoped-permission guard duplication — RESOLVED.** The vendor variant was a
   misnamed alias for `require_native_vendor_context` (membership check, zero
   permission evaluation) whose name satisfied the route-guard architecture
   test. The alias is deleted; the vendor-portal router depends on
   `require_native_vendor_context` explicitly; `/api/v1/vendor` is allowlisted
   as a self-scoped surface; route-level behavior pins added
   (`tests/test_vendor_portal_auth.py`). Granting the vendor surface a real
   RBAC claim (e.g. `vendor:portal:access`) would be a behavior change and
   remains an explicit product decision.
4. **Commit ownership is mixed** across services, `task_session`, `UnitOfWork`, and
   `auth_dependencies` — no single transaction owner yet.
5. **FUP decisions extracted — DONE.** The enforcement sweep (enforce/warn/reset
   hysteresis, repeat-upsell policy, notification fan-out; ~570 lines) moved
   verbatim from the Celery task body to `app/services/fup_enforcement.py`,
   registered as `access.fup_enforcement_sweep` in the SOT relationships
   registry; `app/tasks/usage.py` keeps only task shells, advisory-lock
   plumbing, task names, and queue chaining.
6. **CRM dual-write — RESOLVED.** `crm_native_sync` and its task are removed.
   Verified CRM events enter `integration.inbox`; domain reconcilers are the
   only writers of Sub mirrors and operational consequences.

## Phase 1 next steps (separate PRs, after this ledger is accepted)

1. Pin OpenAPI snapshots + representative generated-client tests for the JSON/mobile
   surfaces (the docs/OpenAPI endpoint is currently live at FastAPI defaults).
2. Pin golden lifecycle scenarios and DB invariants for the critical money/service
   paths (invoice settlement, prepaid credit, suspend/restore).
3. Declare the `subscriber_management` `ProductAssemblySpec` (modules, providers,
   brand, surfaces, compatibility) once the kernel publishes the spec contract.
4. Review the `Subscriber.status` display-mutation sites (finding 1b) — move the
   derived status into a view-model field rather than the ORM attribute.

## Explicitly out of scope for Phase 0

- Adding `tenant_id` columns to any ISP table (needs a separate multi-tenant product
  decision per the adoption plan).
- Renaming or merging `Subscriber`/`Organization` into the kernel Party tables.
- Any dual-write, schema change, or writer replacement.
- Shared database or ORM imports with dotmac_erp or the vendor control plane.
