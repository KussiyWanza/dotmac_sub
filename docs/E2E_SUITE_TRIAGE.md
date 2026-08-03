# Playwright e2e suite triage — why 164 of 228 specs fail on dev

Dated 2026-08-02. Evidence collected against pure `origin/dev` application
code (image `sha-d14543b`) in a disposable container stack, with the suite
run exactly as `.github/workflows/e2e.yml` runs it. Raw failure lists and
run logs live on the throwaway test host; the reproducible facts are below.

## Headline

The nightly E2E (Playwright) workflow has failed on every scheduled run in
the visible history (26 consecutive failures to 2026-08-02; the single
green run was a manually dispatched narrow suite). Because the workflow is
schedule-only and not a required check, the suite rotted silently:
164 of 228 specs fail on plain dev. Almost all of that comes from three
mechanical causes in the test harness (A–C) and one route retirement — not from
164 independent product bugs.

## Root causes, with proof

### A. The admin quick tour opens an `aria-modal` dialog on every fresh context

`templates/layouts/admin.html` auto-starts `static/js/admin-tour.js`
(added 2026-06-12, commit `385a260b9`) 700 ms after DOMContentLoaded
whenever `localStorage['dotmac_admin_tour_seen_v1']` is unset. Every
Playwright spec uses a fresh browser context, so the tour fires on the
first admin page of every spec. Its tooltip is
`role="dialog" aria-modal="true"` — an open aria-modal dialog removes the
rest of the page from the accessibility tree, so every
`get_by_role(...)` locator outside the dialog reports
"element(s) not found", and the full-screen overlay swallows clicks
("element is not visible, enabled and stable" timeouts).

Proof: with the page rendered, `document.querySelector('h1')` finds
"Operations Overview" while `get_by_role("heading", name="Operations
Overview")` finds nothing and `[data-tour-overlay]` is present. Marking the
tour as seen in the fixture flips `test_dashboard.py` + `test_system.py`
from 26 failed to 22 passed / 4 failed with no other change.

Fix (this branch): `tests/playwright/conftest.py` adds an init script to
the staff contexts (`admin_context`, `agent_context`) marking the tour as
seen. Product behaviour is untouched; the tour remains user-visible.

### B. Portal fixtures mint sessions into a session store the app never reads

`customer_context` / `reseller_context` create portal sessions
**runner-side** (`customer_portal.create_customer_session`,
`reseller_portal._create_session`). Those sessions are stored via
`app/services/session_store.py`, which reads `REDIS_URL` — but the root
`tests/conftest.py` deliberately poisons `REDIS_URL` to
`redis://127.0.0.1:9/0`, and the un-poisoning in
`tests/playwright/conftest.py` only happens when `E2E_REDIS_URL` is set.
Neither the CI workflow nor any documented invocation sets `E2E_REDIS_URL`,
and CI's `e2e-redis` container has no host port mapping, so the pytest
process could not reach it anyway. Result: every portal spec's session
cookie points at a session the app cannot see, and every portal page
bounces to `/portal/auth/login?next=...`.

Proof: minting a session inside the app container and requesting
`/portal/dashboard` with the cookie returns 200; the identical mint from a
runner-side process with a reachable shared Redis also returns 200. Setting
`E2E_REDIS_URL` for the suite flips `test_customer_portal.py` +
`test_reseller_portal.py` from 41 failed to 38 passed / 12 failed
/ 3 skipped with no other change.

Fix (this branch): `.github/workflows/e2e.yml` maps
`127.0.0.1:56379 -> e2e-redis:6379` and sets
`E2E_REDIS_URL=redis://127.0.0.1:56379/0` for the pytest step.

### C. The logout specs revoke the shared admin session for the rest of the run

`admin_storage_state` is session-scoped: one UI login serves every admin
spec in the run. `TestLogout` (in `test_auth_flows.py`, alphabetically the
first e2e file) drove `/auth/logout` **through that shared session**,
revoking it server-side — every admin spec that runs afterwards reuses a
dead cookie, bounces to the login page, and fails its first locator. This
is why file-scoped runs pass while full runs collapse: with only the tour
fix applied, `test_dashboard.py` + `test_system.py` go 22 passed / 4
failed when run alone but revert to 26 failed inside the full suite.

Fix (this branch): `TestLogout` performs its own throwaway UI login
(mirroring `TestLogin`) and logs that session out, leaving the shared
state untouched.

### D. `/admin/subscribers` was retired; its 12 specs now land on a 404

`tests/playwright/e2e/test_subscribers.py` still drives
`/admin/subscribers`, which returns the 404 page ("Page not found"); the
customer list lives at `/admin/customers`. The page objects were never
migrated. Similarly `/admin/reports` now redirects to
`/admin/reports/hub`, and some navigation specs still assert the old
sidebar `href`.

Fix: rewrite/retire those page objects against the current routes
(follow-up; not in this branch).

### E. Exact-string drift in page objects

A long tail of specs assert exact strings the templates no longer render,
e.g. `ForgotPasswordPage` expects a heading "Forgot Password" while the
page renders "Forgot password?" (the old capitalisation survives only in
`<title>`). These are individual page-object repairs (follow-up; not in
this branch).

## Residual failures with A + B + C applied

With the three harness fixes on this branch and no application change,
the full suite on pure dev goes from 164 failed / 55 passed (27 min) to
49 failed / 171 passed (9 min). The 49 residuals are the genuine
D/E-class spec drift, concentrated in `test_reseller_portal.py` (8),
`test_permissions.py` (8), `test_auth_flows.py` (7), and a one-to-three
spec tail across twelve files; the exact list lives in the tracking
issue.

## Process finding

Rot accumulated because the workflow was invisible: schedule-only, not
required, and failing nightly with nobody paged. Decision (2026-08-02):
both an alert and a PR gate, plus a failure ratchet so the nightly is
green-by-default instead of permanently red with accepted failures.

## The ratchet, gate, and alert (this branch)

- **Quarantine ratchet** — `tests/playwright/quarantine.json` lists every
  known-failing spec with reason, issue, category, owner, and a
  `review_by` date. The Playwright `conftest.py` applies **strict xfail**
  to listed specs: a known failure reports as xfailed (run stays green),
  while a pass reports as XPASS(strict) = failure, so a repaired spec must
  be removed from the file. Unlisted failures fail the run — the count
  only ratchets down. Entries past `review_by` lose protection and go red
  again until re-reviewed. Setup errors (which xfail cannot cover) may use
  `"action": "skip"`; intermittently-failing specs may use
  `"action": "flaky"` (non-strict xfail) so neither outcome breaks the
  run until they are repaired — expiry still forces review.
- **E2E Gate** (`.github/workflows/e2e-gate.yml`) — an always-reported PR
  check. It passes immediately when the PR touches nothing e2e-relevant,
  and runs the full Playwright suite from the PR's own source when the
  diff touches `templates/`, `static/`, `tests/playwright/`, `app/web/`,
  `app/services/web_*`, `scripts/seed/`, or the e2e workflows. Branch
  protection can require "E2E Gate" without ever waiting on a skipped
  workflow.
- **Nightly alert** (`.github/workflows/e2e.yml`) — one deduplicated
  health issue: created on the first scheduled failure, commented on
  (not duplicated) while red, closed automatically by the first green
  nightly.

## Burn-down order for the 49 quarantined specs

1. Retired/moved routes (`retired-routes`, 9 specs).
2. Authentication headings and locator drift (`auth-drift`, 7).
3. Reseller portal (`reseller-portal`, 8).
4. Permissions (`permissions`, 8).
5. Remaining tail (`tail`, 17 + 1 setup error + 2 flaky).

Each batch removes its quarantine entries in the same change that repairs
the specs — strict XPASS enforces this.
