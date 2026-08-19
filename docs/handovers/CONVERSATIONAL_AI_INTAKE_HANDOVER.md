# Conversational AI Intake Handover

Date: 2026-08-13
Branch: `feature/conversational-ai-intake-v2`

## Goal

Implement governed conversational AI intake for WhatsApp, Facebook Messenger and
Instagram direct messages for Dotmac ISP support. Email, chat widget and public
comment channels remain outside AI intake.

## Confirmed Requirements

- Fresh eligible private-message conversations enter `pending` UI state with a
  durable AI intake session.
- AI identifies itself as `Dotmac Virtual Assistant`, asks bounded
  clarification questions, produces structured routing metadata and requests
  handoff to Team Inbox.
- Team Inbox remains the owner of routing, queueing, assignment, status,
  transfers and provider delivery.
- Round-robin assignment is durable per team and skips offline, inactive or full
  agents. An online presence is considered assignable only when its
  `last_seen_at` observation is no more than three minutes old.
- Default active-conversation capacity is 10, configurable through Comms
  domain settings, with per-agent overrides preserved.
- Queued customers receive configurable, versioned queue notices: initial
  position, position-change updates, heartbeat and handoff.
- NCC DOB/gender cleanup is governed, disabled by default for production
  collection, and saves only through a typed profile cleanup command.

## Architecture Boundaries

- `app.services.ai_intake` owns AI intake eligibility, prompt-safe
  classification, policy resolution and routing metadata.
- `app.services.ai_conversation_intake` owns durable AI session lifecycle,
  policy-version pinning, AI message identity metadata, async session
  processing and cleanup subflow orchestration.
- `app.services.team_inbox_assignment` owns round-robin assignment, capacity and
  FIFO promotion.
- `app.services.team_inbox_queue_notifications` owns queue notice scheduling and
  dedupe evidence.
- `app.services.subscriber_profile_cleanup` owns typed NCC profile cleanup save
  validation.
- Team Inbox outbound remains the provider delivery adapter.

## Current Implementation Status

Local correction work was rebuilt onto current `origin/dev` from the preserved
correction patch. No deployment, merge, production migration or production data
change has been performed.

## Migration Relationship

- Current migration: `alembic/versions/529_conversational_ai_intake.py`
- Current local revision identity: `529_conversational_ai_intake`
- Current local parent: `528_roles_kernel_r1_additive`
- Obsolete local identity `524_conversational_ai_intake` was recreated and must
  not be applied to production.
- Production was observed at `523_domain_settings_tenant_fk` during read-only
  inspection.
- Current development already contains migrations `524_network_map_v2_asset_proposals`
  through `528_roles_kernel_r1_additive`, so AI Intake is additive after
  `528` on this clean v2 branch.
- Excluded local migration: `alembic/versions/520_inbox_self_assign_permission.py`
  because it is self-assign permission work, not conversational AI intake.

## Files Changed

Run this on the branch for the exact committed list:

```bash
git diff --name-only origin/dev...HEAD
```

Expected areas:

- `alembic/versions/529_conversational_ai_intake.py`
- `app/models/ai_intake.py`
- `app/models/team_inbox.py`
- `app/services/ai_intake.py`
- `app/services/ai_conversation_intake.py`
- `app/services/team_inbox_assignment.py`
- `app/services/team_inbox_channel_receive.py`
- `app/services/team_inbox_outbound.py`
- `app/services/team_inbox_queue_notifications.py`
- `app/services/subscriber_profile_cleanup.py`
- `app/tasks/team_inbox.py`
- `app/web/admin/inbox.py`
- `templates/admin/inbox/email_routes.html`
- `templates/components/ui/triage.html`
- SOT docs and focused tests.

## Static Validation Completed Locally

These checks passed before handover on the AI-intake files:

```bash
poetry run ruff check ...
poetry run ruff format --check ...
python -m compileall ...
git diff --check
```

Migration graph parser was run after reparenting and should show
`529_conversational_ai_intake` as the feature migration. On the consolidated
branch, `531_consolidated_open_prs` is the sole head. The customer quote linkage
is revision `530_customer_quote_lead_linkage`, sequenced after AI intake; the
531 revision contains no schema operations.

## Local Test Blocker

Pytest cannot collect in this local environment because the private package
`dotmac_kernel` is unavailable:

```text
ModuleNotFoundError: No module named 'dotmac_kernel'
```

Do not bypass private-registry authentication or paste credentials into the
repo. Use an authenticated development machine/container or CI.

## Night Shift Validation

Run from a non-production development host:

```bash
git fetch origin
git checkout feature/conversational-ai-intake-v2
poetry install --with dev --no-root
poetry run ruff check app tests scripts alembic
poetry run ruff format --check app tests scripts alembic
poetry run mypy app --ignore-missing-imports --no-incremental
poetry run lint-imports
poetry run bandit -r app -c pyproject.toml -q
make test-architecture
make test
TEST_DATABASE_URL=<disposable-postgres-url> make test-integration
```

Also run focused browser/admin checks for:

- AI intake settings form.
- Policy version display/history.
- AI message bubble label and accessibility.
- Queue notification visible behavior.

## Known Limitations

- Functional, migration, concurrency and browser verification is still required
  on an environment with `dotmac_kernel`.
- Production NCC data cleanup collection remains disabled by default and must
  not be enabled without compliance approval.
- Admins must configure explicit provider/account scopes; no global/private
  channel catch-all is allowed for activation.
- The incomplete-task assignment blocker is intentionally not implemented in
  this correction. Team Inbox needs an approved typed eligibility interface from
  the authoritative task domain before adding that cross-domain rule.

## Admin Configuration Required Before Activation

- Provider/account scope per WhatsApp number, Facebook page and Instagram
  account.
- Fallback team.
- Intent definitions and active intent-to-team mappings.
- Welcome message, business tone, approved ISP information and clarification
  questions.
- Queue templates and timings.
- Cleanup prompt, gender public choices, DOB formats and support follow-up team.
- Emergency disable/kill switch review.

## Staging Verification Plan

1. Apply migrations only to a disposable/staging database.
2. Configure one WhatsApp account scope and one test team.
3. Verify email, chat widget and public comments bypass AI.
4. Verify a supported channel with no matching scope bypasses AI.
5. Verify welcome, clarification, handoff and queue messages are AI-labelled.
6. Verify human reply/takeover suppresses in-flight AI output.
7. Verify round-robin cursor survives worker restart.
8. Verify queue updates do not spam unchanged positions.
9. Verify cleanup asks only missing fields and saves only through the typed
   command when production collection is explicitly enabled in test.

## Rollback Plan

- Disable AI intake policies in admin configuration.
- Disable the AI intake session processing scheduled task if necessary.
- Stop queue notices by disabling queue notification scheduled task if needed.
- Revert the feature branch before merge if validation fails.
- If migrations were applied only in staging/test, downgrade
  `529_conversational_ai_intake` there. Do not run production downgrade without
  an approved operator runbook.

## Explicit Warning

Nothing from this branch has been deployed. No production migrations were run.
No production data was modified. AI intake and NCC cleanup were not enabled in
production.

## Next Actions

1. Pull the branch on an authenticated development machine.
2. Run the full validation list above.
3. Review the staged migration against the live dev migration graph.
4. Resolve any test failures before opening a PR.
5. Do not merge, deploy or enable production collection without Michael's
   explicit approval.
