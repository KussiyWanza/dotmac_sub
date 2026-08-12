# Customer Onboarding Launch Checklist

Pre-launch test checklist before bringing real customers onto the platform.
Status as of **2026-06-12**. Update the checkboxes and dates as items are verified.

Legend:
- `[x]` verified working (date + how in parentheses)
- `[ ]` needs testing
- ⚠️ known issue or pending decision — resolve before launch

Suggested order of attack: §0 blockers → §3 money flows with live keys → §1 auth
round-trips → §4 change-plan/usage → one full §9 "new customer" dry run as the
final gate.

---

## 0. Blockers & decisions (do these first)

- [x] Deactivated all 6 synthetic e2e/QA accounts with working portal
      credentials (2026-06-11: admin@example.com, e2e.agent, e2e.user,
      playwright-admin, qa.testcustomer, qa.testreseller — credentials +
      subscriber rows flag-flipped, reversible; 0 synthetic accounts with
      active credentials remain).
- [ ] Cleanup (downgraded from blocker 2026-06-11): 43 active test/E2E
      `catalog_offers` remain in the catalog but are **no longer
      customer-visible** (offer scoping PR #179). Purge at leisure.
- [ ] ⚠️ Decide visibility for the 12 "odd" customer-visible change-plan offers
      (IP blocks, Device Replacement ×2, Fiber Last Mile, 4 leased/dedicated
      plans) — product decision pending.
- [x] Password-reset hardening + UX merged & deployed (2026-06-11, PRs #187 +
      #188): fixed web forgot-form never sending the email; single-use tokens;
      60-min admin TTL cap (15-min for login-redirect tokens); server-side
      8-char minimum; session revocation on reset; rate limit 3/email/15min;
      audit events; working staff/reseller forgot forms; new customer
      self-service page at /portal/auth/forgot-password; 14 new reset tests
      + MFA coverage (reset does not bypass MFA).
- [x] Customer login copy fixed to "Username or email"; removed untrue
      "resets interrupt connectivity" warning (2026-06-11, PR #188).
- [ ] ⚠️ Mobile GLITCHTIP_DSN: backend reporting is already live (DSN found
      in prod .env, app/monitoring.py inits it, server reachable — verified
      2026-06-11). Remaining: pick mobile DSN (reuse backend project vs a
      separate GlitchTip mobile project) — note the DSN is plain http://, which
      mobile release builds block by default (cleartext); HTTPS or a cleartext
      exemption needed — then set in brand.json + rebuild.
- [x] Migration-number coordination resolved (2026-06-12): #192 (MFA lockout)
      and #196 (service extensions) both merged; alembic heads merged on the
      prelaunch branch (commit cf7e82b2), main's single head is 141. Operator
      remainder: run `make docker-migrate` on next deploy to apply on prod DB.

## 1. Auth & account access

- [x] Customer change-password applies only to the local email credential,
      never PPPoE/RADIUS (2026-06-11, verified on prod via rollback test, PR #185)
- [x] Branding/logo on all login, MFA, forgot/reset pages across customer,
      reseller and staff portals (2026-06-11, visual verification, PR #186)
- [ ] Forgot-password round-trip on a real mailbox: email arrives, link
      works, expiry honored (24h customer / 60min staff), token rejected on
      second use, sessions revoked (flow hardened in #187/#188 with automated
      tests; the real-mailbox deliverability pass is what remains)
- [ ] Customer self-service reset page /portal/auth/forgot-password (new in
      #188): full round-trip from the portal login link
- [ ] Portal invite flow: admin customer page → invite email → set password →
      first login
- [ ] MFA: enroll, login challenge, wrong-code rejection — on all three portals.
      Status: PR #192 (login lockout, MFA wrong-code lockout, session epochs +
      30d absolute cap, admin remember-me fix) MERGED 2026-06-12; deploy needs
      its migration (138) + restart. Wrong-code lockout (5 codes / 15 min per
      method) and the admin "Reset MFA" recovery button on the customer page
      shipped in that PR — include both in the manual real-portal pass.
- [ ] Account lockout after 5 failed attempts, 15 min unlock; "account
      disabled" and canceled-subscriber rejections. Shipped in #192 (merged
      2026-06-12), manual pass owed: lock is checked before password verify (no oracle, no indefinite re-locking);
      the customer RADIUS/PPPoE web path gets a per-username attempt throttle
      (10/15min, in-memory per-worker) plus canceled/disabled status checks;
      admin "reset password" now clears an existing lockout.
- [ ] Session expiry/refresh, remember-me duration, logout, revoke-all-sessions
      (mobile sessions screen). Shipped in #192 (merged 2026-06-12), manual
      pass owed: admin remember-me now real
      (session cookies unless ticked — verify a non-remembered admin is logged
      out after browser restart); portal sessions capped at 30 days absolute
      (`customer/reseller_session_absolute_ttl_seconds`); password change/reset
      now also revokes customer/reseller web portal sessions and mobile tokens;
      staff can see/revoke their own sessions via /auth/me/sessions.
- [ ] Mobile biometric app-lock arm/resume cycle on a **real device**.
      Code-reviewed + controller tests added (cold-start re-lock, prompt-loop
      race, resume flash, unlock returns to previous screen). Device script:
      1) slow network, force-kill, relaunch, unlock immediately — must NOT
      re-lock seconds later; 2) arm → background → resume ×5 rapidly (Samsung/
      Xiaomi face unlock especially) — no double prompts; 3) background mid-form
      → unlock — should return to the same screen; 4) app-switcher snapshot
      still shows content (known gap, FLAG_SECURE deliberately not added);
      5) fail biometric 5× — OS passcode fallback must appear; 6) delete all
      fingerprints in OS settings, relaunch — app must open unlocked.

## 2. Service lifecycle & network

- [x] End-to-end activation: create subscriber → provision PPPoE/RADIUS
      credentials → device connects → live session visible in admin
      (2026-06-11 lifecycle/network launch review: PASS; session observability
      PR #142, reaper 1h/15min)
- [ ] ⚠️ Suspend → customer actually loses access (enforcement) → resume
      restores. The 2026-06-11 review found enforcement LEAKY (no CoA
      disconnects ever fired — NAS records lacked the local shared secret —
      ~42 suspended users stayed online). FIXED & MERGED 2026-06-12: #194
      (CoA secret fallback + loud logging), #197 (suspension leak-audit gauge),
      single-writer radcheck (#209), enforcement reconciler (ce93079f) with
      billing-aware suspend guards, and _send_coa_update repair (af2983f4);
      15-min reconciler + hourly password sync firing on cadence, platform
      converged overnight. Remaining: end-to-end manual re-test against a live
      line; operator flip of group_routing_enabled; legacy BSS multi-sub cleanup.
- [ ] Self-service ONT reboot reaches the device (TR-069/GenieACS).
      Notes: only ~17% of ONTs are TR-069-managed (2026-06-11 review);
      PR #198 (5-min per-device cooldown, failed attempts excluded) MERGED
      2026-06-12. Device round-trip on a real TR-069-managed ONT still owed.
- [ ] Self-service WiFi SSID/password change reaches the device
- [ ] Live bandwidth on portal/mobile shows correct download/upload direction
      (rx=upload / tx=download NAS convention — regression-prone; use
      download_bps/upload_bps naming only). Status: portal/mobile paths
      verified correct in review; admin chart showed zeros (schema stripped
      the fields) — fix PR #195 MERGED 2026-06-12. Manual verify of the admin
      chart still owed.

## 3. Billing & money (highest risk)

- [x] Scheduled billing runner produces invoices (fixed + backfilled
      2026-06-09, first scheduled success verified 2026-06-10) —
      **re-verify on the next cycle before launch**
- [ ] Invoice correctness on a real plan: amounts, tax, due date, PDF renders
      with branding (configurable default VAT — off by default — + white-label
      invoice PDF name are in PR #212, OPEN — verify once merged)
- [ ] Paystack with **live keys**: pay an invoice end-to-end including verify
      callback and webhook → invoice marked paid, ledger entry created
- [ ] Flutterwave with **live keys**: same end-to-end
- [ ] Top-up flow (portal + mobile); balance reflects immediately
- [ ] Autopay: save card, enable/disable, charge fires on due date
- [ ] Bank-transfer proof upload → admin verification → payment applied
- [ ] Payment arrangements: create, installment charge, cancel
- [ ] Failure paths: declined card, abandoned webview, double-submit,
      webhook retry idempotency
- [ ] Dunning experience: what a non-paying customer sees (restricted
      dashboard, captive redirect), and recovery after payment (dunning guards
      + autopay + proofs/arrangements back office hardened in #204, merged
      2026-06-12 — manual live-key walkthrough still owed)
- [ ] ⚠️ Outage service-extensions (bulk validity compensation, PR #196,
      MERGED 2026-06-11): post-merge review found a double-apply race (no row
      lock on the pending check — two clicks = 2× compensation days) and an
      unbounded synchronous bulk apply that emits per-subscriber events
      pre-commit (timeout mid-loop → events fired but billing changes rolled
      back). Fix both before using the feature for a real outage; migration
      138 not yet applied to prod.

## 4. Plans, add-ons, usage

- [ ] ⚠️ Change-plan page performance: previously built ~80 proration quotes
      upfront → 46s load / 504 / app crash. A lazy `/change/quote` endpoint
      now exists — explicitly verify page load is fast and submit works
- [ ] Add-on / data-bundle purchase with payment; appears in usage
      immediately; bundle-expiry push notification fires
- [ ] Offer visibility scoping: each reseller's customers see only their
      offers (plan_family + reseller availability, PR #179). Status: PR #191
      (MERGED 2026-06-11) hides archived/drifted offers in portal listings and
      guards the mobile deferred change-plan path; review found the **instant**
      (web) change-plan path still accepts archived-but-is_active offers and
      recommended a one-line data backfill (`is_active=false WHERE status !=
      'active'`) — fast-follow needed.
- [ ] Usage accuracy: portal/mobile usage-summary vs RADIUS accounting on a
      known-traffic test line
- [ ] FUP: approaching/exceeded banners at correct thresholds (portal +
      mobile card); quota enforcement actually throttles/blocks

## 5. Support & notifications

- [ ] Ticket create/comment from portal and mobile → appears in CRM;
      CRM-side reply → visible to customer (bidirectional sync verified
      2026-06-10 — re-smoke before launch)
- [ ] Email deliverability from prod: SPF/DKIM/DMARC for dotmac.ng senders;
      spam-folder check on Gmail and Outlook
- [ ] Push notifications on real Android + iOS devices (FCM): usage alerts,
      bundle expiry. Mobile FCM client merged (#210, 2026-06-12) but inert until
      operator drops in Firebase config; queue runner still OFF in prod by design.
- [ ] SMS channel if enabled (Twilio config) — disabled in prod (no creds)
- [ ] All transactional emails carry branding and correct expiry wording (24h)
      — queued-email branding + dead-sender fix shipped in #200 (merged
      2026-06-12); real-mailbox deliverability pass still owed

## 6. Mobile release readiness

- [ ] Release builds with prod `--dart-define-from-file` — brand.json
      API_BASE_URL points at `https://selfcare.dotmac.io`
- [ ] Store metadata, signed builds, app identity (applicationId / bundle id)
- [ ] Payment deep-link scheme `dotmacpay` returns from gateway webview
      (wired in #201, merged 2026-06-12 — verify on a real gateway round-trip)
- [ ] Token refresh after the app sits idle for days; forced-logout UX when a
      session is revoked server-side

## 7. Security & privacy

- [x] RBAC mount-registry guards: 290 customer-reachable staff routes closed,
      build-failing arch test (PRs #178/#181/#182/#183, deployed 2026-06-10)
- [ ] Resolve pending P1b endpoint-scoping decision. Security sweep done via
      #203 (merged 2026-06-12) + targeted manual pass 2026-06-11 (no new auth
      bypasses; IDOR verified clean; email templates escape user input).
      **P1b still pending product decision**: object-scoped grants (migration
      136) are enforced at the endpoint level but not consistently at object
      level — decide which admin endpoints must additionally enforce
      object-scope so a staff member scoped to customers X,Y can't act on Z.
- [x] Cookies + headers hardened (#203, merged 2026-06-12): app now emits
      HSTS (on HTTPS) + X-Frame-Options/nosniff/Referrer-Policy from a
      middleware, independent of the proxy (the *deployed* nginx had drifted
      from the repo conf and was missing these); `REFRESH_COOKIE_SECURE` is
      now secure-by-default so admin cookies carry `Secure` over TLS.
      **Ops follow-up**: re-sync `/etc/nginx/sites-available/selfcare.dotmac.io`
      with `nginx/selfcare.dotmac.io.conf` (live config lacks the headers);
      verify flags in devtools behind the real domain.
- [x] IDOR sweep done 2026-06-11 — all `/portal/*/{id}` and `/me/*` routes
      enforce ownership in the service layer (invoice/service/installation/
      service-order/arrangement getters filter by account, incl. business
      multi-account via `get_allowed_account_ids`); invoice-PDF verifies
      access before serving. Spot-check one business org during the smoke.
- [x] PPPoE password reveal — confirmed staff-only (admin router is
      `system_user`-gated; customers/resellers cannot reach `customer:read`).
      Now audited (`customer.pppoe_password_reveal`) + per-actor rate-limited
      (30/hr) in #203 (merged 2026-06-12). **Decision pending**: whether to
      narrow from `customer:read` to a dedicated `customer:credential:reveal`
      permission (needs RBAC seeding) — kept `customer:read` for now.
- [x] Login rate limit added (#203, merged 2026-06-12): per-IP middleware on
      all login endpoints (admin/portal/reseller/`/api/v1/auth/login`), default
      20/5min, tunable via `LOGIN_RATE_LIMIT_MAX`/`_WINDOW_SECONDS` — closes the
      credential-stuffing-spray gap the per-account lockout misses. Note:
      in-memory per-worker (×worker-count ceiling). forgot-password 3/email/15min
      (#187); MFA 5/15min, account lockout 5/15min (#192). **No customer-facing
      speedtest exists** (admin-only tool) — that part of the item is N/A.
      Consider a Redis-backed shared limiter + proxy `limit_req` post-launch.

## 8. Performance & capacity

- [x] Monitoring dashboard ~6–10s steady (PRs #85/#86); ingestion OOM fixed —
      no create_engine/Redis-client creation in task hot paths (PR #163)
- [ ] Dashboard / billing / usage pages under realistic concurrency
      (e.g. 200 customers) — note `WEB_CONCURRENCY` currently defaults to
      **1 uvicorn worker**
- [ ] Celery queue depth during an invoice run + notification burst

## 9. Ops & onboarding mechanics

- [ ] **Database backups + a tested restore** (the working tree IS prod via
      bind mount — a bad deploy or stray `rm` is unrecoverable without them)
- [x] Alembic: single head, migrations run as explicit pre-deploy step
      (`make docker-migrate`), never on boot
- [ ] Written deploy runbook: merge → deploy the immutable image → migrate →
      restart the app, Celery workers and beat, bandwidth poller, and syslog
      listener. Where inbound email is enabled, also recreate the profile-gated
      `team-inbox-smtp` service with `make prod-smtp-inbound-up` and require
      `make prod-smtp-inbound-probe` to pass before declaring the intake ready.
      Then enable the reviewed `network_monitoring.channel_health_contracts`
      entry; its warning/critical consequence is immediate, with no shadow mode.
- [ ] Uptime/error alerting for the portal itself (Zabbix watches network
      devices; who watches the app?)
- [ ] Bulk onboarding path: import/create N subscribers + PPPoE creds, batch
      portal invites, RADIUS sync (`app.services.radius_population`)
- [ ] Full "new customer" dry run as the final gate: create → invite →
      login (web + mobile) → pay first invoice → consume data → raise
      ticket → receive notifications
