# DotMac Sub — Administrator Operations Guide

**Version:** 1.1 | **Date:** March 2026 | **Audience:** ISP Operations Staff & System Administrators

This guide covers every configuration option, step-by-step workflow, and operational procedure for running an ISP on DotMac Sub.

Use this guide in three passes:

1. Day 0: finish launch prerequisites, security, billing, and provider setup
2. Day 1: create offers, onboard customers, and validate provisioning
3. Day 2+: run recurring operations, monitor risk, and manage change safely

---

## Table of Contents

1. [Initial System Setup](#1-initial-system-setup)
2. [Company & Branding Configuration](#2-company--branding-configuration)
3. [Authentication & Security Settings](#3-authentication--security-settings)
4. [Creating Tariff Plans (Offers)](#4-creating-tariff-plans-offers)
5. [Fair Usage Policy (FUP) Configuration](#5-fair-usage-policy-fup-configuration)
6. [Billing & Payment Configuration](#6-billing--payment-configuration)
7. [Customer Onboarding (E2E Workflow)](#7-customer-onboarding-e2e-workflow)
8. [PPPoE & RADIUS Configuration](#8-pppoe--radius-configuration)
9. [Network Infrastructure Setup](#9-network-infrastructure-setup)
10. [Provisioning Configuration](#10-provisioning-configuration)
11. [Monitoring & Alert Configuration](#11-monitoring--alert-configuration)
12. [Network Topology Setup](#12-network-topology-setup)
13. [Notification & Communication Setup](#13-notification--communication-setup)
14. [Subscription Lifecycle Management](#14-subscription-lifecycle-management)
15. [Dunning & Collections Configuration](#15-dunning--collections-configuration)
16. [GIS & Coverage Area Management](#16-gis--coverage-area-management)
17. [Reporting Configuration](#17-reporting-configuration)
18. [User & Role Management](#18-user--role-management)
19. [Secrets Management (OpenBao)](#19-secrets-management-openbao)
20. [Integrations & Webhooks](#20-integrations--webhooks)
21. [Scheduler & Background Tasks](#21-scheduler--background-tasks)
22. [Backup & Recovery](#22-backup--recovery)
23. [Settings Reference (All 195+ Settings)](#23-settings-reference)

---

## 1. Initial System Setup

### Pre-requisites Checklist

Before configuring DotMac Sub, ensure:

- [ ] Docker Compose stack is running (`docker compose up -d`)
- [ ] Database migrations applied (`poetry run alembic upgrade heads`)
- [ ] OpenBao secrets seeded (`./scripts/openbao_init.sh`)
- [ ] Initial admin account created
- [ ] SMTP configured (at least Mailhog for testing)

### Launch Order

Apply configuration in this order to avoid circular setup issues:

1. Base platform health: app, database, cache, secrets, email
2. Company identity: branding, company info, portal naming
3. Access and security: auth settings, users, roles, API keys
4. Commercial setup: tax, offers, payment providers, dunning
5. Access network setup: NAS, OLT, RADIUS, provisioning defaults
6. Operational guardrails: notifications, scheduler, backups, health checks

### Change Management Rules

> Billing, RADIUS, and provisioning settings affect live subscribers. Make high-impact changes during a defined change window and record the prior value before saving.

| Area | Risk if changed incorrectly | Minimum safeguard |
|------|-----------------------------|-------------------|
| Billing defaults | Wrong invoice amounts or due dates | Test with a single sandbox subscriber |
| Payment provider keys | Failed collections or duplicate payment confusion | Run a test transaction before go-live |
| PPPoE / RADIUS settings | Mass authentication failures | Validate with one pilot subscriber first |
| Provisioning defaults | Wrong VLAN, profile, or ONT commands | Review generated commands before execution |

### First-Time Setup Steps

1. **Login** → Navigate to `/auth/login`, enter admin credentials
2. **Company Info** → `/admin/system/company-info`
   - Set company name, address, email, phone
   - Set VAT number, registration ID
   - Set bank details (name, account, branch)
3. **Branding** → `/admin/system/branding`
   - Upload sidebar logo (light + dark variants)
   - Upload favicon
   - Set portal display name
4. **Email** → `/admin/system/email`
   - Configure SMTP sender (host, port, credentials)
   - Test connection
5. **Payment Gateway** → `/admin/system/settings-hub` → Billing
   - Set Paystack keys (or Flutterwave)
   - Verify with test transaction
6. **Secrets** → `/admin/system/secrets`
   - Verify required paths plus any configured optional integration paths are populated in OpenBao

### Day-0 Acceptance Checks

- Admin login works without redirect loop or 500 error
- Company details appear correctly in invoice and portal previews
- Test email sends successfully
- A test offer can be created and activated
- A test payment completes end to end
- Health and scheduler pages show expected workers and jobs

---

## 2. Company & Branding Configuration

### Company Information

**URL:** `/admin/system/company-info`

![Company Information](guide_screenshots/ops/ops_01_company_info.png)

| Field | Example | Purpose |
|-------|---------|---------|
| Company Name | Dotmac Technologies | Appears on invoices, emails, portal |
| Street Address | 15 Ahmadu Bello Way | Invoice billing address |
| City | Abuja | |
| State/Region | FCT | |
| Country | Nigeria | |
| Zip/Postal Code | 900001 | |
| Email | admin@dotmac.ng | Contact email |
| Phone | +234 901 234 5678 | Contact phone |
| VAT Number | NG-12345678 | Tax identification |
| Registration ID | RC-123456 | Business registration |
| Bank Name | GT Bank | Payment details on invoices |
| Bank Account | 0123456789 | |
| Bank Branch | Abuja Main | |

### Branding Assets

**URL:** `/admin/system/branding`

![Branding](guide_screenshots/ops/ops_02_branding.png)

| Asset | Recommended Size | Format |
|-------|-----------------|--------|
| Sidebar Logo (Light) | 200×40 px | PNG/SVG |
| Sidebar Logo (Dark) | 200×40 px | PNG/SVG |
| Favicon | 32×32 px | ICO/PNG |

### Portal Names

The portal display name is set via Company Info and appears in:
- Admin portal header
- Customer portal header
- Reseller portal header
- Email "From" name (if not overridden by SMTP sender)

### Branding Decision

Keep branding minimal until operations are stable. Invoice identity, contact details, and payment instructions are more important than cosmetic refinement on launch day.

---

## 3. Authentication & Security Settings

### Session Configuration

**URL:** `/admin/system/settings-hub` → System → Preferences & Security

| Setting | Default | Options | Impact |
|---------|---------|---------|--------|
| JWT Access TTL | 15 min | 1-1440 min | API token expiration |
| JWT Refresh TTL | 30 days | 1-365 days | Refresh token duration |
| Customer Session TTL | 24 hours | 1 hour - 30 days | Portal session length |
| Customer Remember TTL | 30 days | 1-365 days | "Remember me" duration |
| Reseller Session TTL | 24 hours | 1 hour - 30 days | Reseller portal session |
| Login Max Attempts | 5 | 1-50 | Before lockout |
| Lockout Duration | 15 min | 1-1440 min | Lockout period |
| Password Reset Expiry | 60 min | 5-1440 min | Reset link validity |

### Cookie Security

| Setting | Default | When to Change |
|---------|---------|----------------|
| Secure Cookie | false | Set `true` for HTTPS production |
| SameSite | lax | Use `strict` for max security |
| Cookie Domain | (none) | Set for cross-subdomain auth |

### Two-Factor Authentication (2FA)

- Supported methods: TOTP (authenticator app), SMS, Email
- TOTP issuer name configurable (default: `dotmac_sm`)
- Admin can disable MFA for a locked-out user via User Management

### Security Baseline for Production

- Enable secure cookies under HTTPS
- Reduce session lifetimes if teams share workstations
- Enforce MFA for privileged operators
- Limit API keys by owner and purpose
- Review lockout and password reset expiry after go-live

---

## 4. Creating Tariff Plans (Offers)

### Step-by-Step: Create a New Internet Plan

**URL:** `/admin/catalog/offers` → **+ New Offer**

![Catalog Offers](guide_screenshots/ops/ops_13_catalog_offers.png)

#### Step 1: Basic Information

| Field | Example | Options |
|-------|---------|---------|
| Name | Home Fiber 50Mbps | Free text |
| Description | Residential 50/20 Mbps fiber | Free text |
| Service Type | residential | `residential`, `business` |
| Plan Category | internet | `internet`, `recurring`, `one_time`, `bundle` |
| Status | active | `active`, `inactive`, `archived` |
| Show on Customer Portal | Yes | Checkbox — visible to customers for self-service plan change |

#### Step 2: Speed Configuration

| Field | Example | Notes |
|-------|---------|-------|
| Download Speed (Mbps) | 50 | Sets RADIUS Mikrotik-Rate-Limit |
| Upload Speed (Mbps) | 20 | |
| Guaranteed Speed Type | none | `none`, `relative`, `fixed` |
| Guaranteed % | 80 | Only if type = relative |

#### Step 3: Pricing

| Field | Example | Options |
|-------|---------|---------|
| Price Type | recurring | `recurring`, `one_time`, `usage` |
| Amount | 15000.00 | In base currency (NGN) |
| Currency | NGN | ISO 4217 |
| Billing Cycle | monthly | `daily`, `weekly`, `monthly`, `quarterly`, `annual` |
| Tax Application | exclusive | `exclusive` (add tax on top), `inclusive` (tax included), `exempt` |

#### Step 4: Billing Mode

| Mode | Description | When to Use |
|------|-------------|-------------|
| **Prepaid** | Customer funds exact taxed contracted service periods before renewal; canonical coverage and funding decide access | Residential customers, pay-as-you-go |
| **Postpaid** | Customer pays collectible invoice receivables under dunning policy | Business customers, credit terms |

#### Step 5: Contract Terms

| Term | Description |
|------|-------------|
| Month-to-month | No commitment, cancel anytime |
| 12-month | 1-year contract with early termination fee |
| 24-month | 2-year contract |

#### Step 6: RADIUS Profile (Optional)

Link a RADIUS reply profile to control:
- Mikrotik-Rate-Limit (speed)
- Framed-Pool (IP pool)
- Service-Type
- Simultaneous-Use (authentication concurrency check)
- Delegated-IPv6-Prefix-Pool

### Plan Variants

To offer the same service at different speeds/prices:
1. Create a base offer (e.g., "Home Fiber 25Mbps" at ₦10,000)
2. Create upgrade tiers (50Mbps at ₦15,000, 100Mbps at ₦25,000)
3. All share the same Service Type = `residential`
4. Customers can change between tiers via the portal (with proration)

### Add-On Services

Add supplementary services to any plan:

| Add-On Type | Example | Billing |
|-------------|---------|---------|
| static_ip | Static IPv4 Address | ₦2,000/month recurring |
| router_rental | CPE Router Rental | ₦1,500/month recurring |
| install_fee | Installation Fee | ₦15,000 one-time |
| premium_support | Priority Support | ₦5,000/month recurring |
| extra_ip | Additional IP Block (/29) | ₦10,000/month recurring |
| managed_wifi | Managed WiFi Service | ₦3,000/month recurring |
| custom | Custom add-on | Any amount |

### Offer Design Guardrails

Before publishing an offer, verify:

- pricing matches the intended tax application
- billing cycle matches the collection model
- customer-portal visibility is deliberate
- the RADIUS profile and speed values agree
- the offer naming is clear enough for support and finance teams

---

## 5. Fair Usage Policy (FUP) Configuration

### Step-by-Step: Configure FUP for a Plan

**URL:** Go to offer detail → **FUP** tab

#### Step 1: Create FUP Policy

Each offer can have ONE FUP policy with multiple rules.

**Policy Settings:**
| Setting | Options | Default |
|---------|---------|---------|
| Traffic Accounting Start | HH:MM | 00:00 |
| Traffic Accounting End | HH:MM | 23:59 |
| Inverse Interval | true/false | false (count traffic inside window) |
| Consumption Period | monthly, daily, weekly | monthly |

#### Step 2: Add FUP Rules

Rules are evaluated in `sort_order` sequence:

| Rule Field | Options | Example |
|------------|---------|---------|
| Name | Free text | "Warn at 80%" |
| Threshold | Number | 80 |
| Data Unit | `mb`, `gb`, `tb` | gb |
| Direction | `up`, `down`, `up_down` | up_down |
| Action | `reduce_speed`, `block`, `notify` | notify |
| Speed Reduction % | 0-100 | 50 (for reduce_speed) |
| Sort Order | Integer | 1 (lower = first) |
| Time Start | HH:MM (optional) | 08:00 |
| Time End | HH:MM (optional) | 22:00 |
| Days of Week | 0-6 CSV (optional) | 0,1,2,3,4 (Mon-Fri) |
| Enabled By Rule | UUID (optional) | Chain from another rule |

#### Step 3: Rule Chaining Example

**Scenario:** Warn at 80%, throttle at 100%, block at 120%

| Sort | Rule | Threshold | Action | Chain |
|------|------|-----------|--------|-------|
| 1 | Soft Warning | 80 GB | notify | — |
| 2 | Speed Throttle | 100 GB | reduce_speed (50%) | Enabled by Rule #1 |
| 3 | Hard Block | 120 GB | block | Enabled by Rule #2 |

**Rule #2 only fires after Rule #1 has fired** (chain dependency).

#### Step 4: Time-of-Day Rules

**Scenario:** Peak-hour throttle (6pm-midnight) but unlimited off-peak

| Sort | Rule | Threshold | Time | Days | Action |
|------|------|-----------|------|------|--------|
| 1 | Peak Warning | 50 GB | 18:00-00:00 | 0-6 | notify |
| 2 | Peak Throttle | 75 GB | 18:00-00:00 | 0-6 | reduce_speed (70%) |
| 3 | Off-Peak Fair Use | 200 GB | 00:00-18:00 | 0-6 | notify |

#### Step 5: Test with FUP Simulator

**URL:** Offer detail → FUP tab → **Simulate & Test**

The simulator lets you:
1. Set hypothetical usage (GB slider)
2. Set time of day
3. Set day of week
4. Set billing period position
5. **See which rules would trigger** — visual pass/fail per rule with chain status

---

## 6. Billing & Payment Configuration

### Invoice Settings

**URL:** `/admin/system/settings-hub` → Billing

![Billing Config](guide_screenshots/ops/ops_05_billing_config.png)

| Setting | Default | Purpose |
|---------|---------|---------|
| Default Currency | NGN | All invoices |
| Payment Due Days | 14 | Days until invoice is overdue |
| Invoice Number Prefix | INV- | Invoice numbering format |
| Invoice Number Padding | 6 | Zero-padding (INV-000001) |
| Credit Note Prefix | CR- | Credit note numbering |
| Minimum Invoice Amount | 0.00 | Skip invoices below this |
| Proration Enabled | true | Prorate mid-cycle changes |
| Auto-Activate on Billing | true | Activate pending subs when billed |

### Tax Configuration

**URL:** `/admin/system/config/tax`

| Setting | Options | Default |
|---------|---------|---------|
| Tax Application | exclusive, inclusive, exempt | exclusive |
| Tax rates are per-offer configurable | | |

**Tax Modes:**
- **Exclusive** — Tax added on top of price (₦15,000 + 7.5% VAT = ₦16,125)
- **Inclusive** — Tax included in price (₦15,000 includes VAT)
- **Exempt** — No tax applied

### Payment Gateway Setup

**URL:** `/admin/system/settings-hub` → Billing → Payment section

#### Paystack Configuration

| Setting | Value | Where to Get |
|---------|-------|-------------|
| Secret Key | `sk_live_...` or `sk_test_...` | Paystack Dashboard → Settings → API |
| Public Key | `pk_live_...` or `pk_test_...` | Same |
| Provider Type | paystack | |

Store keys in OpenBao: `bao://secret/paystack#secret_key`

#### Flutterwave (Backup)

| Setting | Value |
|---------|-------|
| Secret Key | `FLWSECK-...` |
| Public Key | `FLWPUBK-...` |
| Secret Hash | Webhook verification hash |
| Failover Enabled | true (auto-switch if primary fails) |

### Financial access enforcement

Financial enforcement has no enable switch or hard-coded global day ladder.
Postpaid consequences follow the active dunning policy; prepaid consequences
follow exact coverage, funding, contracted renewal terms, account-specific
grace, and the shared time-of-day window. Payment receipt alone does not promise
restoration: the owner recomputes settlement or funding and clears only the
eligible reason-scoped lock.

The complete current contract and operator workflow lives only in
`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`.

### Billing Configuration Smoke Test

Use one controlled subscriber to verify:

1. invoice generation
2. online payment creation
3. payment allocation
4. overdue transition
5. suspension and reactivation behavior

---

## 7. Customer Onboarding (E2E Workflow)

### Step 1: Create Subscriber

**URL:** `/admin/customers` → **+ New**

| Field | Required | Example |
|-------|----------|---------|
| First Name | Yes | John |
| Last Name | Yes | Doe |
| Email | Yes | john.doe@gmail.com |
| Phone | No | +234 801 234 5678 |
| POP Site | Recommended | Garki POP |
| Category | No | residential / business |
| Status | Auto | active |

### Step 2: Create Subscription

**URL:** Customer detail → **+ Add Subscription**

| Field | Options | Example |
|-------|---------|---------|
| Offer | From catalog | Home Fiber 50Mbps |
| Billing Mode | prepaid / postpaid | prepaid |
| Start Date | Date picker | Today |

**On creation, the system automatically:**
1. Generates PPPoE credentials (e.g., `105000003` / random password)
2. Syncs to external RADIUS (FreeRADIUS radcheck/radreply/radusergroup)
3. Sets speed limits via Mikrotik-Rate-Limit RADIUS attribute
4. Emits `subscription.activated` event
5. Queues welcome notification email
6. Generates prorated invoice (if mid-cycle)

### Correct an accidentally activated subscription

When an active subscription has an older restorable sibling, its detail page
shows **Correct Subscription**. Use it only to repair an accidental activation:

1. Open the mistakenly active subscription and select **Correct Subscription**.
2. Find the owner-reviewed form for the exact subscription and plan that should
   remain. The form shows the full target subscription ID and creation time so
   repeated plan names are distinguishable. Blocked options explain why they
   cannot run.
3. Review the cancellation/restoration pair, PPPoE credential, target RADIUS
   profile and speed, FUP cleanup, and financial-history result.
4. Tick the exact-correction confirmation and submit the shared action form.
5. Open the restored subscription and verify its provisioning evidence and
   external FreeRADIUS rows.

The command does not delete history and does not guess the correct plan. It
cancels the mistaken service, restores the selected service, moves the single
active access credential to the target profile, clears stale FUP runtime state,
and requests the normal RADIUS/IP reconciliation after commit. It makes no
automatic credit or invoice adjustment. If the mistaken subscription already
has an invoice line, the preview blocks the command; reconcile the financial
documents through their owning billing workflow before retrying.

Do not use ordinary **Restore** for this scenario. Restore is intentionally
blocked when another active subscription already uses the same PPPoE login.
Open that active subscription and use **Correct Subscription**. The correction
preview also blocks malformed stored IPv4/IPv6 values, a missing or mismatched
PPPoE username, an unconfigured target speed profile, or an active target
enforcement lock. Repair the named evidence through its owning workflow, then
refresh the preview; do not bypass the preflight with direct database writes.

For staging acceptance, use the disposable Playwright fixture rather than a
real customer. Set `PLAYWRIGHT_BASE_URL` to the staging URL and
`TEST_DATABASE_URL` to that deployment's disposable test database, then run
`poetry run pytest -q tests/playwright/e2e/test_subscription_correction.py`.
The suite refuses known production hosts and creates a unique customer with one
mistaken active plan, one older stopped Unlimited Lite target, one PPPoE
credential, and an exact 15 Mbps profile before exercising the UI correction.

### Step 3: Verify RADIUS Sync

Check that credentials are synced:
1. Go to subscriber detail → view PPPoE credentials
2. Check external RADIUS DB: `/admin/system/config/radius`
3. Verify `radcheck` table has the username + Cleartext-Password
4. Verify `radreply` table has Mikrotik-Rate-Limit

### Step 4: Generate Invoice

**Automatic:** If billing mode is prepaid, invoice is generated on activation.

**Manual:** Customer detail → **Generate Invoice** button per subscription.

### Step 5: Customer Self-Service

Customer logs in at `/portal` with PPPoE username + password:
- Views subscription, speed, billing
- Pays online via Paystack
- Changes plan (with proration)
- Opens support tickets
- Runs speed tests

### Onboarding Completion Checklist

- Customer contact details verified
- Offer and billing mode verified
- PPPoE credentials confirmed in subscriber record
- RADIUS sync confirmed
- Invoice or balance state confirmed
- Portal login tested where applicable
- Installation or provisioning evidence saved

---

## 8. PPPoE & RADIUS Configuration

### PPPoE Auto-Generation

**URL:** `/admin/system/settings-hub` → Network → RADIUS

![RADIUS Config](guide_screenshots/ops/ops_10_radius_config.png)

| Setting | Default | Purpose |
|---------|---------|---------|
| Auto-Generate Enabled | false | Generate PPPoE on subscription creation |
| Username Prefix | 1050 | Prefix for auto-generated usernames |
| Username Padding | 5 | Zero-padding width |
| Username Start | 1 | Starting sequence number |
| Password Length | 12 | Auto-generated password length |

**Example:** Prefix `1050`, padding 5, start 1 → `105000001`, `105000002`, ...

### RADIUS Check/Control Attributes

| Attribute | Example | Purpose |
|-----------|---------|---------|
| Cleartext-Password | managed secret | Authenticate the PPPoE login |
| Simultaneous-Use | 1 | Maximum concurrent sessions for one login |

`Simultaneous-Use` belongs in `radcheck`. It is deliberately cut over through
the database-owned `radius.simultaneous_use_enforcement_enabled` setting, which
defaults to `false`. Before enabling it, complete the stale-session and
shared-credential checks in
`docs/designs/RADIUS_SIMULTANEOUS_USE_CUTOVER.md`.

### RADIUS Reply Attributes

For each RADIUS profile linked to an offer, these attributes are synced:

| Attribute | Example | Purpose |
|-----------|---------|---------|
| Mikrotik-Rate-Limit | 50M/20M | Download/upload speed |
| Framed-Pool | pool-residential | IP address pool |
| Service-Type | Framed-User | RADIUS service type |
| Framed-Protocol | PPP | Connection protocol |
| Delegated-IPv6-Prefix-Pool | pool-v6 | IPv6 pool |

### RADIUS Sync Configuration

| Setting | Default | Purpose |
|---------|---------|---------|
| Sync Users | true | Sync subscriber credentials to RADIUS |
| Sync NAS Clients | true | Sync NAS devices to RADIUS |
| Auth Port | 1812 | RADIUS authentication port |
| Accounting Port | 1813 | RADIUS accounting port |

### Captive Portal Redirect

For suspended subscribers:

| Setting | Default | Purpose |
|---------|---------|---------|
| Captive Redirect Enabled | false | Enable captive redirect |
| Captive Portal IP | — | IP/CIDR for captive network |
| Captive Portal URL | — | Redirect URL for suspended users |

### Change of Authorization (CoA)

| Setting | Default | Purpose |
|---------|---------|---------|
| CoA Enabled | true | Send CoA on speed/profile changes |
| CoA Timeout | 3 sec | CoA request timeout |
| CoA Retries | 1 | Retry count on failure |
| Refresh on Profile Change | true | Auto-refresh active sessions |

### Production Rollout Advice

Turn on auto-generated PPPoE and CoA only after validating naming, pool assignment, and profile behavior with a pilot group. Incorrect defaults at this layer fail loudly and at scale.

---

## 9. Network Infrastructure Setup

### NAS Device Configuration

**URL:** `/admin/network/nas` → **+ New NAS**

![NAS Devices](guide_screenshots/ops/ops_19_nas_devices.png)

| Field | Example | Options |
|-------|---------|---------|
| Name | Garki-MikroTik-Core | Free text |
| IP Address | 10.0.1.1 | Management IP |
| Vendor | mikrotik | mikrotik, huawei, ubiquiti, cisco, juniper, cambium, nokia, zte, other |
| RADIUS Shared Secret | (encrypted) | Stored in OpenBao |
| Connection Type | pppoe | pppoe, dhcp, ipoe, static, hotspot |
| SSH Credentials | (encrypted) | For provisioning commands |
| API Credentials | (encrypted) | For RouterOS API |
| SNMP Community | (encrypted) | For monitoring |
| Status | active | active, maintenance, offline, decommissioned |

### IP Pool Configuration

Each NAS should have linked IP pools:

| Field | Example |
|-------|---------|
| Pool Name | pool-residential-garki |
| IP Version | ipv4 or ipv6 |
| NAS Device | Garki-MikroTik-Core |
| Linked to NAS | Yes (nas_device_id FK) |

### OLT Configuration

**URL:** `/admin/network/olts` → **+ New OLT**

| Field | Example |
|-------|---------|
| Name | Garki-Huawei-OLT |
| IP Address | 10.0.2.1 |
| Vendor | Huawei |
| SSH Username | admin |
| SSH Password | (encrypted) |
| SNMP Community | public |
| Model | MA5800-X7 |

### ONT Management

**URL:** `/admin/network/onts`

ONT lifecycle:
1. **Discover** — Import from GenieACS TR-069 or manual entry
2. **Assign** — Link ONT to subscriber + OLT port
3. **Pre-flight** — Run 9-point validation check
4. **Provision** — Generate OLT CLI commands (T-CONT, GEM, service-port, VLAN, WAN)

### Network Cutover Checklist

- Device reachable on management IP
- Credentials stored in OpenBao, not plain text
- SNMP and SSH/API access confirmed
- VLAN and IP pools defined before onboarding subscribers
- One live test subscriber validated on each new access platform

---

## 10. Provisioning Configuration

### Service Order Workflow

| Status | Description | Action |
|--------|-------------|--------|
| draft | Created, not submitted | Edit details |
| submitted | Ready for scheduling | Assign technician |
| scheduled | Appointment set | Dispatch |
| provisioning | OLT commands executing | Monitor |
| active | Service live | Complete |
| failed | Error during provisioning | Retry/fix |
| canceled | Order canceled | Archive |

### Provisioning Settings

| Setting | Default | Options |
|---------|---------|---------|
| Default Vendor | other | mikrotik, huawei, zte, nokia, genieacs, other |
| Default SO Status | draft | draft, submitted, scheduled |
| Default Task Status | pending | pending, in_progress, blocked, completed, failed |

### Fiber Cost Configuration

| Setting | Default | Purpose |
|---------|---------|---------|
| Drop Cable Cost/Meter | ₦2.50 | For cost estimation |
| Labor Cost/Meter | ₦1.50 | |
| ONT Device Cost | ₦85.00 | |
| Installation Base Fee | ₦50.00 | |
| Quote Approval Threshold | ₦5,000 | Quotes above this need approval |
| Quote Validity Days | 30 | Quote expiration |

---

## 11. Monitoring & Alert Configuration

### Device Health Thresholds

**URL:** `/admin/system/settings-hub` → Network → Monitoring

![Monitoring Config](guide_screenshots/ops/ops_12_monitoring_config.png)

![Monitoring Dashboard](guide_screenshots/ops/ops_24_monitoring.png)

| Setting | Default | Purpose |
|---------|---------|---------|
| Disk Warning % | 80 | Server disk warning |
| Disk Critical % | 90 | Server disk critical |
| Memory Warning % | 80 | Memory warning |
| Memory Critical % | 90 | Memory critical |
| Load Warning | 1.0 | Load per core warning |
| Load Critical | 1.5 | Load per core critical |

### ONT Signal Thresholds

| Setting | Default | Purpose |
|---------|---------|---------|
| Signal Warning (dBm) | -25 | ONT optical warning |
| Signal Critical (dBm) | -28 | ONT optical critical |
| Alert Cooldown (min) | 30 | Suppress repeated alerts |

### Polling Intervals

| Setting | Default | Purpose |
|---------|---------|---------|
| OLT Polling | 5 min | OLT SNMP poll frequency |
| Device Ping | 120 sec | Core device ping |
| SNMP Walk | 300 sec | Interface discovery |
| Alert Evaluation | 60 sec | Alert rule check frequency |
| Metrics Retention | 90 days | DeviceMetric cleanup |

### Creating Alert Rules

**URL:** `/admin/network/alarms` → **+ New Rule**

| Field | Options | Example |
|-------|---------|---------|
| Rule Name | Free text | "Core Router CPU High" |
| Metric Type | cpu, memory, temperature, rx_bps, tx_bps, uptime, custom | cpu |
| Operator | gt, gte, lt, lte, eq | gt |
| Threshold | Number | 85 |
| Duration (sec) | Integer | 300 (5 min sustained) |
| Severity | info, warning, critical | critical |
| Device | Select from list | Garki-Core-Router |
| Interface | Optional | GigabitEthernet0/0/1 |

### Alert Notification Routing

When alerts trigger:
1. Check **AlertNotificationPolicy** steps (escalation chain)
2. Check **OnCallRotation** members
3. Fallback to **admin user emails**

Configure at `/admin/notifications` → Alert Policies

### Operational Priorities

Start with alerts that protect revenue and service continuity:

1. core device offline
2. OLT / ONT signal degradation
3. payment or notification backlog
4. system disk, memory, and queue pressure

Delay lower-value informational alerts until the team can handle noise without missing critical events.

---

## 12. Network Topology Setup

### Creating Topology Links

**URL:** `/admin/network/topology` → **Add Link**

![Topology](guide_screenshots/ops/ops_26_topology.png)

| Field | Options | Example |
|-------|---------|---------|
| Source Device | Dropdown | Garki-Core-Router |
| Source Interface | Auto-populated from SNMP | GigabitEthernet0/0/1 |
| Target Device | Dropdown | Lagos-Core-Router |
| Target Interface | Auto-populated | GigabitEthernet0/0/2 |
| Link Role | uplink, backhaul, peering, lag_member, crossconnect, access, distribution, core | backhaul |
| Medium | fiber, wireless, ethernet, virtual | fiber |
| Capacity (bps) | Number | 10000000000 (10 Gbps) |
| Bundle Key | Optional | lag-abuja-lagos (groups parallel links) |
| Topology Group | Optional | core-ring |
| Admin Status | enabled, disabled, maintenance | enabled |

### LAG / Parallel Links

To represent a LAG:
1. Create 2+ links between the same device pair
2. Set all links to the same `Bundle Key` (e.g., `lag-garki-core`)
3. Set link_role to `lag_member`
4. The topology graph renders them as curved parallel arcs

### Topology Groups

Use groups to organize the topology view:
- `core-ring` — backbone links
- `abuja-access` — Abuja access layer
- `lagos-distribution` — Lagos distribution

Filter the topology page by group using the dropdown.

---

## 13. Notification & Communication Setup

### SMTP Configuration

**URL:** `/admin/system/email`

![Email Config](guide_screenshots/ops/ops_09_email_config.png)

#### Per-Sender Profiles

Create multiple SMTP senders for different purposes:

| Sender Key | Host | From Email | Activity |
|------------|------|-----------|----------|
| default | smtp.sendgrid.net | noreply@dotmac.ng | General notifications |
| billing | smtp.sendgrid.net | billing@dotmac.ng | Invoice emails |
| support | smtp.gmail.com | support@dotmac.ng | Support tickets |

#### Activity-Based Routing

| Activity | Description | Sender Key |
|----------|-------------|------------|
| notification_queue | System notifications | default |
| billing_invoice | Invoice emails | billing |
| subscription_welcome | Welcome emails | default |
| auth_password_reset | Password reset links | default |
| auth_user_invite | User invitations | default |

### SMS Configuration

| Setting | Provider Options |
|---------|-----------------|
| SMS Provider | twilio, africas_talking, webhook |
| API Key | Provider-specific |
| API Secret | Provider-specific |
| From Number | +234... |
| Webhook URL | For generic webhook provider |

### Notification Templates

**URL:** `/admin/notifications/templates`

39 pre-configured templates organized by category:

**Subscription:** subscription_created, subscription_activated, subscription_suspended, subscription_canceled, subscription_expiring

**Billing:** invoice_issued, invoice_due_7d, invoice_due_1d, invoice_overdue, payment_received, dunning_notice, suspension_warning, service_suspended

**Provisioning:** work_order_scheduled, technician_assigned, work_order_completed

**Support:** ticket_created, ticket_updated, ticket_resolved

**Network:** ont_offline, ont_online, ont_signal_degraded

Each template has:
- **Code** — unique identifier
- **Channel** — email or sms
- **Subject** — with `{variable}` placeholders
- **Body** — HTML for email, plain text for SMS
- **Active** — enable/disable

### Template Variables

Available in all templates:

| Variable | Source | Example |
|----------|-------|---------|
| `{subscriber_name}` | Subscriber record | John Doe |
| `{invoice_number}` | Invoice record | INV-000042 |
| `{amount}` | Invoice/payment | ₦15,000.00 |
| `{due_date}` | Invoice | Mar 15, 2026 |
| `{plan_name}` | Offer name | Home Fiber 50Mbps |
| `{portal_url}` | System config | /portal |
| `{device_serial}` | ONT record | HWTC12345678 |
| `{usage_percent}` | FUP calculation | 85 |
| `{days_remaining}` | Subscription | 7 |
| `{grace_hours}` | Billing setting | 48 |

### Communications Quality Checklist

- Invoice and dunning templates use the correct sender profile
- Reset and invite emails are delivered from a domain users trust
- SMS fallbacks are configured for urgent service-impacting notifications
- Variables are previewed before activating edited templates

---

## 14. Subscription Lifecycle Management

### Status Transitions

```
pending → active → suspended → active (reactivate)
                 → canceled
                 → expired
```

| From | To | Trigger | Automatic? |
|------|----|---------|-----------|
| pending | active | First invoice paid or admin activation | Both |
| active | suspended | Confirmed lifecycle consequence from canonical financial or access policy | Both |
| active | canceled | Admin or customer request | Manual |
| active | expired | End date reached | Yes (Celery task) |
| suspended | active | Confirmed restoration after the originating reason is resolved | Both |
| any | canceled | Admin action | Manual |

### Plan Changes

**Admin:** Customer detail → subscription → Change Plan

**Customer Portal:** Services → Change Plan

| Billing Mode | On Upgrade | On Downgrade |
|-------------|------------|-------------|
| Prepaid | Canonical prepaid plan-change debit from available funding | Canonical reviewed credit/adjustment outcome |
| Postpaid | Next invoice reflects new rate | Next invoice reflects new rate |

**Proration Calculation:**
```
daily_rate = plan_price / cycle_days
credit = daily_rate × days_remaining (old plan)
charge = daily_rate × days_remaining (new plan)
net = charge - credit
```

---

## 15. Dunning & Collections Configuration

**Admin surfaces:** `/admin/billing/collections` and
`/admin/system/settings-hub` → Billing.

Configure dunning policy, account/policy grace, prepaid minimum funding,
currency, notification templates, task cadence, and the shared local
enforcement start/end time through their owning admin surfaces. Core lifecycle
work cannot be disabled, and there is no weekend, holiday, activation,
readiness, or health bypass.

Do not copy an enforcement ladder, balance formula, reconstruction command, or
repair procedure from this general admin guide. The single current ownership,
decision, dry-run/apply, lock/RADIUS, retry, daily-review, and incident contract
is [Financial access enforcement](FINANCIAL_ACCESS_ENFORCEMENT.md).

---

## 16. GIS & Coverage Area Management

### Coverage Areas

**URL:** `/admin/gis` → Areas tab → **+ New Area**

| Field | Example |
|-------|---------|
| Name | Garki Coverage Zone |
| Type | coverage / service_area / region |
| GeoJSON Polygon | Paste from geojson.io or draw on map |

### Coverage Check API

Verify if an address is within a service area:
```
GET /api/v1/gis/coverage-check?latitude=9.06&longitude=7.49
```

Returns:
```json
{
  "covered": true,
  "matching_areas": [{"name": "Garki Coverage Zone", "area_type": "coverage"}]
}
```

### Geocoding

**URL:** `/admin/system/tools/geocode`

| Provider | Config | Free Tier |
|----------|--------|-----------|
| Nominatim | Self-hosted (Docker) | Unlimited |
| Google Maps | API key required | $200/month free |
| Mapbox | Token required | 100K/month free |

---

## 17. Reporting Configuration

### Available Reports

**URL:** `/admin/reports/hub`

| Report | URL | Filters |
|--------|-----|---------|
| Revenue | `/admin/reports/revenue` | 30/90/365 days, CSV export |
| Subscribers | `/admin/reports/subscribers` | Date range, CSV export |
| Churn Analysis | `/admin/reports/churn` | Date range |
| Bandwidth & Usage | `/admin/reports/bandwidth` | 7/30/90 days |
| MRR Net Change | `/admin/reports/mrr` | Year filter |
| Network | `/admin/reports/network` | CSV export |
| Technician | `/admin/reports/technician` | Date range |
| Revenue by Category | `/admin/reports/revenue-categories` | — |
| Custom Pricing | `/admin/reports/custom-pricing` | — |

---

## 18. User & Role Management

### Creating Admin Users

**URL:** `/admin/system/users` → **+ New User**

![Users](guide_screenshots/ops/ops_33_users.png)

| Field | Example |
|-------|---------|
| First Name | Sarah |
| Last Name | Ofikwu |
| Email | s.ofikwu@dotmac.ng |
| User Type | system_user |
| Role | Admin / Viewer / Custom |

### RBAC Permissions

Permission format: `resource:action`

| Permission | Grants |
|-----------|--------|
| `subscriber:read` | View customers |
| `subscriber:write` | Create/edit customers |
| `subscriber:impersonate` | View as Customer |
| `billing:read` | View invoices/payments |
| `billing:write` | Create/edit invoices |
| `network:read` | View network devices |
| `network:write` | Configure network |
| `monitoring:read` | View monitoring dashboard |
| `monitoring:write` | Create alert rules |
| `system:read` | View system settings |
| `system:write` | Modify system settings |
| `system:settings:read` | View settings hub |
| `system:settings:write` | Modify settings |

### Access Review Cadence

- Daily: verify no emergency shared credentials are still in use
- Weekly: review new API keys and user invites
- Monthly: audit privileged roles, disabled users, and MFA exceptions

---

## 19. Secrets Management (OpenBao)

### Secret Paths

**URL:** `/admin/system/secrets`

![Secrets Management](guide_screenshots/ops/ops_35_secrets.png)

| Path | Fields | Used By |
|------|--------|---------|
| secret/auth | jwt_secret, totp_encryption_key, credential_encryption_key, wireguard_key_encryption_key | Legacy auth/bootstrap compatibility |
| secret/settings/auth | credential_encryption_key, credential_encryption_previous_key, rotation timestamps | Scheduled credential rotation |
| secret/paystack | secret_key, public_key | Payment gateway when configured |
| secret/database | url, password | Database connection |
| secret/redis | password, url, broker_url, result_backend | Cache/broker |
| secret/radius | db_password, db_dsn | FreeRADIUS sync |
| secret/genieacs | mongodb_dsn, mongodb_password, jwt_secret, cwmp_user, cwmp_pass | TR-069 ACS |
| secret/s3 | access_key, secret_key | MinIO storage |
| secret/migration | smartolt_api_key | Data migration |
| secret/notifications | smtp_host, smtp_port, smtp_username, smtp_password, sms_api_key, sms_api_secret | Email/SMS when configured |
| secret/integrations/meta_social | facebook_page_access_token, instagram_login_access_token, webhook_signing_secret, webhook_verify_token | Facebook Page and Instagram inbox transport |

The inventory expands OpenBao KV v2 folders and displays editable leaf paths.
For example, `integrations/` appears as
`secret/integrations/meta_social`, with field names only; existing values remain
masked and are never returned to the browser.

### Reference Format

Use in integration installation secret bindings: `bao://secret/<path>#<field>`

Example: Paystack installation binding `gateway_credentials` =
`bao://secret/paystack#secret_key`.

### Rotating Secrets

Ordinary provider secrets can be updated in OpenBao. Application processes
refresh cached values within `OPENBAO_CACHE_TTL_SECONDS` (60 seconds by
default).

The credential-at-rest Fernet key is different: do not replace it directly.
`app.tasks.security.run_scheduled_credential_rotation` checks daily and rotates
on the configured interval. It publishes the new current key together with the
previous key, rewrites known encrypted fields, retains the previous key for the
grace period, and runs a final convergence pass before retirement.

Automatic rotation requires `CREDENTIAL_ENCRYPTION_KEY` to be unset or to be an
OpenBao reference such as
`bao://secret/settings/auth#credential_encryption_key`. A literal environment
key makes the task report `blocked_static_environment_key`; this prevents one
worker changing data while other processes remain pinned to an old key.

For the one-time migration, first run the credential backfills and a full
rotation dry-run with the current literal key. Set
`CREDENTIAL_ENCRYPTION_KEY_SEED` to that exact existing key, byte for byte. Do
not generate a new key during an upgrade: existing `enc:` values would become
undecryptable. Run `scripts/setup/openbao_init.sh`, confirm
`secret/settings/auth#credential_encryption_key` resolves to the same key, then
change `CREDENTIAL_ENCRYPTION_KEY` to the reference above, remove the seed
variable, and recreate every API, worker, beat, poller, and listener process.
Run the rotation dry-run again after the restart. Never rerun initialization
with an old seed value.

The daily rotation task is also the credential-integrity observer, including
on days when rotation is not due. It publishes redacted, bounded state through
the shared observability service. Use these series in VictoriaMetrics/Grafana:

- `observability_state{domain="credentials",signal="undecryptable",scope="all"}`
- `observability_state{domain="credentials",signal="plaintext",scope="all"}`
- `observability_state{domain="credentials",signal="one_way",scope="all"}`
- `observability_snapshot_age_seconds{domain="credentials"}`
- `observability_snapshot_status{domain="credentials",status="error"}`

The scan never exports credential values or record identifiers. Non-zero
plaintext or undecryptable totals also enter the standard admin-alert lifecycle;
undecryptable values block automatic key rotation until corrected.
The `one_way` state covers valid RADIUS crypt, PBKDF2, and opaque migrated hashes.
These values are intentionally preserved and are not remediation candidates.

Use the same inventory for one-time plaintext remediation. Dry-run first, then
execute, then confirm a second dry-run reports zero plaintext candidates:

```bash
python -m scripts.one_off.remediate_credential_encryption --dry-run
python -m scripts.one_off.remediate_credential_encryption --execute
python -m scripts.one_off.remediate_credential_encryption --dry-run
```

The command returns aggregate JSON only and publishes the resulting integrity
snapshot. It blocks before writing when the active key is missing or any stored
ciphertext is undecryptable.

If the original key cannot be recovered, resolve undecryptable values through
the lifecycle cleanup planner before plaintext remediation. The planner:

- decommissions and clears an inactive NAS only when it has no non-terminal
  subscription and no live RADIUS session by device ID or client IP;
- recovers a service-referenced or active NAS secret only when every configured
  external FreeRADIUS store agrees on one authoritative value;
- removes only the corrupt `desired_config.wifi.password` leaf for an ONT and
  records that an operator reset is required;
- stages one audit event per mutation in the same local database transaction;
- emits an identity-free plan digest and refuses a different execute-time plan.

Review the aggregate plan, then pass its exact digest:

```bash
python -m scripts.one_off.cleanup_unrecoverable_credentials
python -m scripts.one_off.cleanup_unrecoverable_credentials \
  --execute --confirm-plan-digest <reviewed-digest>
python -m scripts.one_off.cleanup_unrecoverable_credentials
```

Do not use this workflow to make an active device look decommissioned or to
invent a replacement secret. Resolve lifecycle blockers first. After cleanup,
run plaintext remediation and confirm both `plaintext` and `undecryptable` are
zero. Search the audit log for action `credential_lifecycle_cleanup` to review
record-level evidence without exposing secrets in command output.

NAS records recovered from authoritative FreeRADIUS may still have lifecycle
or subscription-link drift. Use the NAS lifecycle reconciler rather than
editing `status`, `is_active`, subscription NAS links, or RADIUS clients
independently:

```bash
python -m scripts.one_off.reconcile_nas_lifecycle
python -m scripts.one_off.reconcile_nas_lifecycle --details
python -m scripts.one_off.reconcile_nas_lifecycle \
  --execute --confirm-plan-digest <reviewed-digest>
```

The reconciler uses administrative intent, non-terminal subscriptions, exact
subscription-bound live sessions, fresh native monitoring, and internal plus
external RADIUS state. It can automatically reactivate only when live-session
or fresh monitoring evidence supports the existing active intent. It can
relink only when every affected subscription has one exact, active live-session
NAS. FreeRADIUS presence alone never reactivates a device. Ambiguous records
produce `manual_review`, block execution, publish the `nas_lifecycle`
observability snapshot, and enter the admin-alert lifecycle. Detailed output
contains network-device identity and aggregate service counts, never customer
identity or credential values. Successful writes use action
`nas_lifecycle_reconcile` in the transactional audit log.

For records blocked on `manual_review`, gather recent accounting evidence before
changing lifecycle or subscription links:

```bash
python -m scripts.one_off.report_nas_access_path_evidence
python -m scripts.one_off.report_nas_access_path_evidence --days 90 --details
```

The report is read-only. It resolves accounting rows through their direct NAS
identity first and their linked RADIUS client only as a fallback. Output is
aggregated by NAS and never includes subscriber, subscription, credential, or
session identity. Recommendations are review classifications, not mutation
authority: historical use can support reactivation or relink review, but only
current exact live-session evidence can authorize automatic relinking.
The command reports `source_stale` and exits `2` when the newest accounting
observation exceeds `radius_accounting_source_stale_seconds` (one hour by
default). Do not use its recommendations until the accounting import is healthy
and the report has been rerun.

FreeRADIUS uses dedicated `FREERADIUS_DB_*` environment variables. They must
identify the same database as the active FreeRADIUS connector, never the app's
`DATABASE_URL` or generic application database settings. Before deploying this
change to an existing environment, seed those variables from the current
connector target, run `freeradius -XC` in the container, and verify that both
`radpostauth` and `radacct` advance. The accounting importer fails its task when
the source is stale, allowing task-reliability alerting to remain authoritative.

Existing RADIUS databases must also accept the full 253-octet NAS-Port-Id
payload. Vendor interface descriptions longer than the historical 32-character
default otherwise reject the complete accounting row. Apply the idempotent
upgrade before or with the application deployment:

```bash
docker exec -i dotmac_radius_pg_test sh -lc \
  'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < config/freeradius/upgrade_003_radacct_nasportid_capacity.sql
```

Verify `radacct.nasportid` reports `253` in `information_schema.columns`, then
confirm `radius_radacct_schema_ok` is `1`. Rejected over-length writes are
labelled in Loki as `event="radius_accounting_write_rejected"`; that event
must remain absent after the upgrade.

### Rotation Order

Rotate non-customer-facing secrets first, then payment, then authentication or network secrets. Keep one verified rollback path for each secret family before moving to the next.

---

## 20. Integrations & Webhooks

### Webhook Events

**URL:** `/admin/system/webhooks`

40+ event types available:

| Category | Events |
|----------|--------|
| Subscriber | created, updated, suspended, reactivated |
| Subscription | created, activated, suspended, resumed, canceled, upgraded, downgraded, expiring |
| Invoice | created, sent, paid, overdue |
| Payment | received, failed, refunded |
| Usage | recorded, warning, exhausted, topped_up |
| Provisioning | started, completed, failed |
| Network | device.offline, device.online, session.started, session.ended, network.alert |

### Webhook Security

- HMAC-SHA256 signature in `X-Webhook-Signature-256` header
- 10 retry attempts with exponential backoff (1min → 8hrs)
- Delivery tracking per webhook

### Integration Rollout Order

1. Enable the webhook endpoint in staging or a test receiver
2. Verify signature validation
3. Replay a small set of events
4. Confirm idempotency on the receiving side
5. Only then enable production delivery

### Accounting Integrations

| Provider | Sync Capabilities |
|----------|------------------|
| QuickBooks Online | Invoices, payments, customers (bidirectional) |
| Xero | Invoices, payments (bidirectional) |
| Sage | Stub (not yet implemented) |

---

## 21. Scheduler & Background Tasks

### Core Celery Tasks

| Task | Default Interval | Purpose |
|------|-----------------|---------|
| Billing Cycle | 86400s (daily) | Generate invoices |
| Dunning | 86400s (daily) | Collections enforcement |
| Prepaid Enforcement | 86400s (daily, opt-in) | Balance checks |
| Usage Rating | 86400s (daily) | Usage charge generation |
| Expiry Reminders | 86400s (daily) | Subscription renewal reminders |
| Subscription Expiration | 86400s (daily) | Expire past-due subscriptions |
| Notification Queue | 60s (1 min) | Deliver queued emails/SMS |
| RADIUS Sync | configurable | Sync to external RADIUS |
| Device Ping | 120s (2 min) | Core device reachability |
| SNMP Refresh | 300s (5 min) | Device health + interfaces |
| Alert Evaluation | 60s (1 min) | Check alert thresholds |
| OLT Polling | 300s (5 min) | OLT/ONT signal polling |
| GIS Sync | 3600s (hourly) | Sync locations/addresses |
| Bandwidth Stream | 5s | Process MikroTik queue samples |
| Metrics Cleanup | 86400s (daily) | Delete old DeviceMetric records |
| WireGuard Log Cleanup | 86400s (daily) | Purge old VPN logs |

### Daily / Weekly / Monthly Cadence

| Cadence | What to review |
|---------|----------------|
| Daily | health page, alert queue, overdue invoices, failed notifications, failed provisioning jobs |
| Weekly | scheduler drift, device polling health, RADIUS sync health, backup success |
| Monthly | secrets rotation posture, user access review, payment provider reconciliation, retention cleanup |

### Viewing Task Status

**URL:** `/admin/system/scheduler`

---

## 22. Backup & Recovery

### Database Backup

```bash
# Create remote folder structure
./scripts/init_dotmac_sub_backup_dirs.sh

# Manual backup
./scripts/backup_dotmac_sub_dbs_to_rclone.sh

# Install cron (daily at 6pm)
./scripts/install_dotmac_sub_backup_cron.sh
```

Backup script:
- Creates per-database folders in `Backup:db.backup/dotmac_sub/`
- Dumps PostgreSQL databases via `pg_dump`
- Dumps MongoDB databases via `mongodump --archive --gzip`
- Compresses with gzip
- Uploads to rclone remote (S3, GCS, etc.)
- Keeps last 5 backups (configurable `KEEP_LAST`)

### OpenBao Backup

Use the OpenBao storage backend's authenticated snapshot procedure and store the
snapshot in encrypted backup storage. Do not export KV values into plaintext
JSON. Supply the backup token at runtime or through a mounted token file; never
place a default token in scripts or documentation.

---

## 23. Settings Reference

All 195+ settings organized by domain. See the Settings Hub at `/admin/system/settings-hub` for the full interactive list with current values.

### Quick Settings Lookup

| Domain | Settings Count | URL |
|--------|---------------|-----|
| Auth | 24 | `/admin/system/config/preferences` |
| Audit | 5 | `/admin/system/config/preferences` |
| Billing | 49 | `/admin/system/settings-hub?category=billing` |
| Catalog | 18 | `/admin/system/settings-hub?category=billing` |
| Subscriber | 10 | `/admin/system/settings-hub` |
| Usage/FUP | 10 | `/admin/system/config/fup` |
| Collections | 16 | `/admin/system/config/finance-automation` |
| Notification | 17 | `/admin/system/config/monitoring` |
| Network | 31 | `/admin/system/config/cpe` |
| Monitoring | 14 | `/admin/system/config/monitoring` |
| RADIUS | 23 | `/admin/system/config/radius` |
| Provisioning | 7 | `/admin/system/settings-hub` |
| GIS | 9 | `/admin/system/settings-hub` |
| Bandwidth | 9 | `/admin/system/settings-hub` |

---

*DotMac Sub Administrator Operations Guide v1.1 — March 2026*
