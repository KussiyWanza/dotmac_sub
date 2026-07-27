# Playwright E2E Suite

## Recommended Structure

- `tests/playwright/conftest.py` - Playwright/pytest fixtures and storageState setup
- `tests/playwright/helpers/` - API helpers, auth setup, test data factories
- `tests/playwright/pages/` - Page Objects (admin, customer, reseller, auth)
- `tests/playwright/e2e/` - E2E specs (smoke, lifecycle, permissions)
- `tests/playwright/.auth/` - Generated storageState files (ignored by git)

## Prerequisites

1. Use an isolated checkout on the explicitly named `seabone` test server. Do not
   run this suite from the live staging checkout.
2. Run the app against a disposable database (the default browser URL is
   `http://localhost:8000` on `seabone`).
3. Seed the RBAC catalogue, then create the canonical system admin if needed:

```bash
poetry run python -m scripts.seed.seed_rbac
poetry run python -m scripts.seed.seed_admin \
  --email admin@example.com \
  --first-name Admin \
  --last-name User \
  --username admin
```

The admin seeder prompts for the password without echoing it. It creates a
`SystemUser`, its local credential, and a global admin-role assignment in one
transaction; it never creates a customer `Subscriber`.

## Environment Variables

Required:

- `E2E_ADMIN_USERNAME`
- `E2E_ADMIN_PASSWORD`

Optional:

- `PLAYWRIGHT_BASE_URL` (default: `http://localhost:8000`)
- `PLAYWRIGHT_BROWSER` (default: `firefox`)
- `PLAYWRIGHT_HEADLESS` (default: `true`)
- `PLAYWRIGHT_TIMEOUT_MS` (default: `10000`)
- `PLAYWRIGHT_NAV_TIMEOUT_MS` (default: `15000`)
- `PLAYWRIGHT_EXPECT_TIMEOUT_MS` (default: `10000`)
- `PLAYWRIGHT_LINK_CHECK_MAX` (default: `200`) - cap on pages scanned in link smoke tests
- `TEST_DATABASE_URL` - disposable database used only to prepare test state that
  has no public setup API; never point it at a shared or production database
- `E2E_AGENT_USERNAME` (default: `e2e.agent`)
- `E2E_AGENT_PASSWORD` (default: `AgentPass123!`)
- `E2E_USER_USERNAME` (default: `e2e.user`)
- `E2E_USER_PASSWORD` (default: `UserPass123!`)
- `TOTP_ENCRYPTION_KEY` - Fernet key the app uses to encrypt TOTP secrets. Required
  for the reseller MFA journeys: without it `/reseller/profile/mfa/setup` returns
  HTTP 500 ("TOTP encryption key not configured"), so `test_mfa_setup_page_loads`
  and `test_mfa_confirm_rejects_invalid_code` are skipped. Set the same value the
  app process uses. Generate one with
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

The suite will create agent/user identities via the admin API when they do not exist.

## Running the Suite

```bash
PLAYWRIGHT_BROWSER=firefox \
E2E_ADMIN_USERNAME=admin@example.com \
poetry run pytest tests/playwright/e2e
```

Load `E2E_ADMIN_PASSWORD` from the approved test-secret source before running the
command; do not place the value in shell history or checked-in files.

## What’s Covered

- Admin authentication through the real `/auth/login` web flow and session cookie
- Agent/user authentication via `/api/v1/auth/login`
- Role-based access checks (admin-only APIs, impersonation permission)
- Portal smoke coverage for admin, customer, reseller, and auth pages
- Smoke coverage for internal links in admin, customer portal, and public pages

## Notes

- MFA must be disabled for E2E users.
- Storage states are generated at runtime under `tests/playwright/.auth/`.
- Admin storage state is captured only after an external browser login succeeds and
  redirects to `/admin/dashboard`; the fixture does not mint sessions by calling
  application internals or opening the application database.
- Some legacy journey specs still reference page objects that are not currently implemented; keep documentation and tests in sync before enabling them in CI.
