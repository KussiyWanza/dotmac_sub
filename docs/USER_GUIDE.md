# DotMac Sub — User Guide

**Version:** 1.1 | **Date:** March 2026 | **For:** ISP Administrators & Technical Staff

This guide is optimized for daily operators. Start with the quick-start checklist and the common task paths, then jump to the detailed feature sections when you need exact screens or terminology.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Getting Started](#2-getting-started)
3. [Admin Dashboard](#3-admin-dashboard)
4. [Customer Management](#4-customer-management)
5. [Catalog & Subscriptions](#5-catalog--subscriptions)
6. [Billing & Payments](#6-billing--payments)
7. [Network Management](#7-network-management)
8. [Provisioning](#8-provisioning)
9. [Monitoring & Alerts](#9-monitoring--alerts)
10. [Network Topology](#10-network-topology)
11. [GIS & Coverage](#11-gis--coverage)
12. [Reports & Analytics](#12-reports--analytics)
13. [Notifications](#13-notifications)
14. [Customer Portal](#14-customer-portal)
15. [Reseller Portal](#15-reseller-portal)
16. [System Administration](#16-system-administration)
17. [Secrets Management](#17-secrets-management)
18. [Technical Setup](#18-technical-setup)
19. [Troubleshooting](#19-troubleshooting)

---

## 1. System Overview

DotMac Sub is a multi-tenant subscription management system for ISPs and fiber network operators. It handles the complete customer lifecycle from signup through billing, provisioning, and monitoring.

**Core Flow:** Customer → Subscription → PPPoE → RADIUS → Billing → Provisioning → Monitoring

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI (Python 3.12) |
| Database | PostgreSQL 16 + PostGIS |
| Frontend | Jinja2 + HTMX + Alpine.js + Tailwind CSS v4 |
| Task Queue | Celery + Redis |
| Metrics | VictoriaMetrics |
| Secrets | OpenBao (Vault-compatible) |
| TR-069 | GenieACS |

### Portal URLs

| Portal | URL | Purpose |
|--------|-----|---------|
| Admin | `/admin` | Full system management |
| Customer | `/portal` | Subscriber self-service |
| Reseller | `/reseller` | Partner management |
| API | `/api/v1` | REST API (JWT auth) |

![Login Page](guide_screenshots/01_login.png)

### Who This Guide Is For

- Front-desk and back-office operators who create customers, subscriptions, and invoices
- Billing teams who monitor overdue balances and payment recovery
- Technical teams who assign ONTs, manage PPPoE, and verify live services
- Supervisors who need dashboard, reporting, and operational health visibility

### Common Task Paths

| I need to... | Go to | Jump to |
|--------------|-------|---------|
| Create a customer and activate service | Customers → Subscription → Provisioning | Sections 4, 5, and 8 |
| Issue or review invoices and payments | Billing | Section 6 |
| Assign or troubleshoot ONTs / PPPoE | Network | Section 7 |
| Check alarms, usage, and device health | Monitoring / Reports | Sections 9 and 12 |
| Help a subscriber use the portal | Customer Portal | Section 14 |

---

## 2. Getting Started

### First Login

1. Navigate to your domain's `/auth/login` page
2. Enter your admin credentials (username + password)
3. You'll land on the **Admin Dashboard**

The sidebar navigation has three groups:
- **Core** — Dashboard, Customers, Billing, Catalog
- **Operations** — Network, Provisioning, GIS, VPN
- **Insights** — Reports, Notifications, System

### First 15 Minutes Checklist

1. Confirm company branding and email settings under **System**
2. Check the dashboard for open invoices, tickets, and recent system activity
3. Verify offers exist before creating live subscriptions
4. Confirm PPPoE / RADIUS defaults before activating internet services
5. Check that payment providers are configured before sending customers to the portal

### Before You Make a Live Change

> Treat billing, PPPoE, and provisioning changes as production operations. Confirm the subscriber, service, effective date, and rollback path before saving.

| Change Type | Verify First | Typical Risk |
|-------------|--------------|--------------|
| Plan change | Billing mode, proration, effective date | Unexpected invoice or credit |
| Suspension / reactivation | Outstanding balance and communication history | Service restored or blocked incorrectly |
| PPPoE credential reset | Subscriber identity and active session status | Session collision or support call spike |
| ONT re-assignment | Correct serial number and target port | Wrong customer mapped to hardware |

---

## 3. Admin Dashboard

The dashboard displays key business metrics at a glance.

![Admin Dashboard](guide_screenshots/03_dashboard.png)

**KPI Cards:**
- Active Subscribers — total with active status
- Monthly Revenue — payments received this month
- Open Invoices — unpaid invoices
- Open Tickets — support tickets pending

**Recent Events Table** shows the latest system activity.

---

## 4. Customer Management

### Subscriber List

Navigate to **Customers** in the sidebar.

![Customers](guide_screenshots/05_customers.png)

- Search by name, email, phone, account number
- Filter by status (Active, Suspended, Blocked)
- Click any row for subscriber details

### Creating a Subscriber

1. Click **+ New Subscriber**
2. Fill in: First Name, Last Name, Email, Phone
3. Select a **POP Site** (determines NAS assignment)
4. Click **Create**

The system generates a unique subscriber number and associates the customer with their POP site for provisioning.

### Recommended Customer Creation Order

1. Create the customer record with correct contact information
2. Confirm service address and POP site
3. Add the subscription only after selecting the correct offer
4. Review generated PPPoE credentials before sharing them
5. Trigger invoice or activation only after verifying billing mode

---

## 5. Catalog & Subscriptions

### Managing Offers

Navigate to **Catalog > Offers**.

![Catalog Offers](guide_screenshots/08_catalog_offers.png)

Each offer defines: name, service type, speed, pricing (recurring), billing mode (prepaid/postpaid), and FUP policy.

### Creating a Subscription

1. Go to customer detail → **+ Add Subscription**
2. Select an **Offer** from the catalog
3. Set billing mode and start date
4. Click **Create**

The system automatically generates PPPoE credentials, syncs to RADIUS, and emits a welcome notification.

### Subscription Sanity Checks

Use this quick review before saving a subscription:

| Item | What good looks like |
|------|----------------------|
| Offer | Correct speed, billing cycle, and tax mode |
| Billing mode | Matches how the customer is expected to pay |
| Start date | Aligned with install date or service handover |
| Portal visibility | Enabled only for plans you want customers to self-manage |
| RADIUS profile | Present when access control or speed policy is required |

### Fair Usage Policy (FUP)

![FUP Configuration](guide_screenshots/10_fup_rules.png)

Configure per-plan data limits with threshold rules, time windows, day filters, and rule chaining. Use the **Simulate & Test** tab to preview behavior.

---

## 6. Billing & Payments

### Billing Overview

![Billing](guide_screenshots/06_billing_overview.png)

Metrics: total billed, outstanding, overdue. Monthly revenue trend chart.

### Invoices

![Invoices](guide_screenshots/07_invoices.png)

**Lifecycle:** Draft → Issued → Overdue → Paid

Invoices are auto-generated on subscription activation, billing cycle completion, and plan changes (proration).

### Dunning & Auto-Suspension

1. Invoice becomes overdue → warning email sent
2. Grace period (configurable, default 48 hours)
3. After grace → subscriber suspended, RADIUS credentials removed
4. Payment received → auto-reactivation

### Daily Billing Checks

- Review draft and overdue invoices at the start of each workday
- Confirm payments are allocated before manually reactivating service
- Use notes or communication history before overriding an automatic suspension
- Validate proration outcomes after upgrades or downgrades on active subscriptions

---

## 7. Network Management

### OLTs

![OLTs](guide_screenshots/11_network_olts.png)

Manage fiber OLT infrastructure with SSH credentials, connected ONTs, and port utilization.

### ONTs

![ONTs](guide_screenshots/12_network_onts.png)

ONT lifecycle: Import from TR-069 → Assign to subscriber → Pre-flight check (9 points) → Provision.

### TR-069

![TR-069](guide_screenshots/15_network_tr069.png)

Manage CPE devices via GenieACS: sync, link, reboot, factory reset, parameter refresh.

### NAS Devices

![NAS](guide_screenshots/16_network_nas.png)

Configure RADIUS clients with shared secrets (encrypted via OpenBao).

### Quick Troubleshooting Flow

1. Confirm the subscriber has an active subscription
2. Check PPPoE username and password on the subscriber record
3. Verify the RADIUS client / NAS exists and is active
4. Review recent network or session errors
5. Confirm the assigned ONT or CPE is mapped to the correct subscriber

---

## 8. Provisioning

![Provisioning](guide_screenshots/17_provisioning.png)

Service order workflow: Created → Scheduled → Provisioning → Active.

The 13-step ONT provisioning orchestrator generates Huawei OLT CLI commands for T-CONT, GEM port, service profile, VLAN, WAN, and PPPoE configuration.

### Safe Provisioning Sequence

1. Validate subscriber, offer, and access technology
2. Confirm OLT, port, VLAN, and ONT serial assignment
3. Run pre-flight checks before pushing commands
4. Save generated commands or evidence for rollback
5. Verify service activation from both provisioning and subscriber views

---

## 9. Monitoring & Alerts

![Monitoring Dashboard](guide_screenshots/13_network_monitoring.png)

### Dashboard Components
- Device Status KPIs (online/offline/degraded)
- Device Health Table (CPU, memory, temperature, uptime)
- Bandwidth Overview with top consumers
- ONU Status Charts
- VPN Tunnel Status
- Active Alarms

### Alert Rules

Create threshold-based alerts with metric type, operator, threshold, duration, and severity. When alerts trigger, notifications are sent via the escalation chain: Policy → On-call → Admin fallback.

### What Needs Immediate Attention

| Signal | Why it matters | First response |
|--------|----------------|----------------|
| Many subscribers drop at once | Likely shared infrastructure issue | Check POP, NAS, OLT, and topology alarms |
| Single ONT offline after install | Provisioning or physical issue | Review provisioning job and optical levels |
| Repeated high CPU / memory on a device | Risk of service degradation | Inspect device metrics and recent config changes |
| Notification queue backlog | Customer comms are delayed | Check Celery worker health and SMTP/SMS settings |

---

## 10. Network Topology

![Network Topology](guide_screenshots/14_network_topology.png)

D3.js force-directed graph replacing the legacy weathermap:

- **Nodes** = devices (color-coded by status)
- **Edges** = links (color-coded by utilization)
- Parallel links rendered as curved arcs
- Click nodes for drilldown panel
- Filter by POP site and topology group

### Creating Links

1. Click **Add Link**
2. Select source/target device and interface
3. Set role, medium, capacity
4. Optional: bundle key, topology group

---

## 11. GIS & Coverage

![GIS Map](guide_screenshots/18_gis_map.png)

Interactive Leaflet.js map with:
- Color-coded markers (POP, Address, Customer, Asset)
- Coverage area polygon editor
- Batch geocoding tool
- Coverage check API: `GET /api/v1/gis/coverage-check?latitude=&longitude=`

---

## 12. Reports & Analytics

![Reports Hub](guide_screenshots/19_reports_hub.png)

### Available Reports

| Report | Description |
|--------|-------------|
| Revenue | Collection rate, payment trends |
| Subscribers | Growth, churn analysis |
| Bandwidth & Usage | Per-plan usage, top consumers |
| MRR Net Change | Monthly recurring revenue |
| Technician | Job completion, first-visit rate |

![Revenue Report](guide_screenshots/20_reports_revenue.png)

![Bandwidth Report](guide_screenshots/21_reports_bandwidth.png)

Every report opened from the Reports Hub provides an export or regulatory
download action. CSV exports preserve the report's active filters and include
the complete matching result set where the screen is paginated. Regulatory
filing reports retain their purpose-built XLSX or PDF downloads.

---

## 13. Notifications

![Notification Templates](guide_screenshots/23_notifications_templates.png)

39 pre-configured templates covering subscription lifecycle, billing, provisioning, and network events. Channels: Email (SMTP), SMS (Twilio/Africa's Talking), WhatsApp.

### Delivery Pipeline

Event → Handler → Template Lookup → Queue → Celery Worker → SMTP/SMS → Retry (max 3)

![Notification Queue](guide_screenshots/24_notifications_queue.png)

### Operator Tip

Use templates and queue history together. If a subscriber says they were not notified, check the template state, the queue entry, and the final delivery status before resending.

---

## 14. Customer Portal

![Portal Login](guide_screenshots/40_portal_login.png)

![Portal Dashboard](guide_screenshots/41_portal_dashboard.png)

### Login

Customers may sign in with their customer number or PPPoE username. A contact
email also works with the portal password when it belongs to only one customer
account with an active portal credential. Email matching ignores letter case.

If the email is shared by more than one customer, the portal does not choose an
account. Use the customer number instead. Suspended customers may still sign in;
disabled and canceled customers may not.

### Features

| Feature | Description |
|---------|-------------|
| Dashboard | Balance, next bill, service status |
| Services | Plans, FUP usage, PPPoE credentials |
| Billing | Invoices, PDF download, online payment (Paystack) |
| Usage | Real-time bandwidth, daily usage table |
| Speed Test | Browser-based with history |
| Support | Ticket creation and tracking |

![Portal Services](guide_screenshots/42_portal_services.png)

![Portal Billing](guide_screenshots/43_portal_billing.png)

For suspended subscribers: restricted dashboard with outstanding balance, warning banner on all pages, pay online → auto-reactivation.

### Portal Support Checklist

- Confirm the subscriber can log in with the correct identifier and password
- For a shared contact email, ask the customer to use their customer number
- Verify invoices are issued before troubleshooting online payment
- Check that the plan is marked visible if self-service change is expected
- Confirm portal warnings match the subscriber's actual balance and status

---

## 15. Reseller Portal

![Reseller Dashboard](guide_screenshots/51_reseller_dashboard.png)

- Dashboard with alert notifications (overdue, suspended, new accounts)
- Account management with search
- Invoice history per account
- Revenue report with Chart.js bar chart
- Customer impersonation ("View as Customer")

![Reseller Revenue](guide_screenshots/53_reseller_revenue.png)

---

## 16. System Administration

![Settings Hub](guide_screenshots/25_system_settings.png)

| Area | Purpose |
|------|---------|
| Users & Roles | RBAC admin accounts |
| Branding | Logos, favicon, colors |
| Email/SMTP | Mail transport configuration |
| API Keys | API access tokens |
| Secrets | OpenBao vault management |
| Legal | Terms, privacy policy |
| Scheduler | Celery task management |

![System Users](guide_screenshots/26_system_users.png)

---

## 17. Secrets Management

![Secrets](guide_screenshots/27_system_secrets.png)

All credentials stored in OpenBao KV v2:

| Path | Contents |
|------|----------|
| `secret/auth` | JWT, TOTP, credential encryption keys |
| `secret/paystack` | Payment gateway keys |
| `secret/database` | Connection URL, password |
| `secret/redis` | Broker credentials |
| `secret/radius` | RADIUS DB password |
| `secret/genieacs` | MongoDB, CWMP auth |
| `secret/s3` | MinIO access keys |

Reference format: `bao://secret/<path>#<field>`

---

## 18. Technical Setup

### Docker Services

| Service | Port | Purpose |
|---------|------|---------|
| App | 8001 | FastAPI web server |
| PostgreSQL | 5434 | Main database |
| Redis | 6379 | Cache + broker |
| OpenBao | 8200 | Secrets vault |
| Mailhog | 8025 | Email testing |
| FreeRADIUS | 1812/1813 | RADIUS auth |
| GenieACS | 7547/3000 | TR-069 ACS |
| VictoriaMetrics | 8428 | Time-series |
| MinIO | 9000/9001 | S3 storage |

### Quick Start

```bash
docker compose up -d
poetry install
poetry run alembic upgrade heads
./scripts/openbao_init.sh
make dev
```

### Quality Commands

```bash
make lint          # Ruff linting
make type-check    # Mypy
make test          # pytest
make check         # All quality checks
```

### Environment Health Checklist

- App responds on `http://localhost:8001`
- Mailhog responds on `http://localhost:8025`
- Database migrations are current
- OpenBao secrets are populated
- Background workers are running before testing billing, notifications, or provisioning

---

## 19. Troubleshooting

| Issue | Solution |
|-------|----------|
| Login 500 error | Check `issue_web_session_token` is module-level call |
| Email not sending | Check SMTP at `/admin/system/email` — host not `disabled.invalid` |
| RADIUS DNS error | Use `localhost:5437` not Docker hostname |
| PPPoE collision | Prefix is `1050` — check sequence in settings |
| OpenBao unavailable | `docker compose up -d openbao` |
| Paystack not configured | Set keys in OpenBao at `secret/paystack` |

### Checking Logs

```bash
docker logs dotmac_sub_app -f --tail 100
docker logs dotmac_sub_celery_worker -f --tail 50
```

### Email Testing

Visit Mailhog at `http://localhost:8025`:

![Mailhog](guide_screenshots/60_mailhog.png)

### System Health

Navigate to `/admin/system/health`:

![System Health](guide_screenshots/28_system_health.png)

---

*Generated from DotMac Sub v1.1 — March 2026*
