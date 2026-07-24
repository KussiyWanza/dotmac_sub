# DotMac Sub — UI/UX Master Plan

> **Status: historical module inventory, not a current specification.** Page,
> KPI, chart, table, card-grid, and action proposals below must be revalidated
> against `UI_INFORMATION_AND_ACTION_STANDARD.md`, the current domain SOT, and
> `PRODUCTION_UI_BRIEF.md` before implementation. This document does not
> authorize a new metric, business calculation, action, or data owner.

## Architecture Principles

### Branding Owns Color

Palette names in the module specifications below are historical design
references, not implementation sources of truth. New and touched surfaces use
branding-generated `primary-*` and `accent-*` identity tokens and the shared
`status-*` semantic roles. Services and API payloads emit semantic roles or
categorical keys, never literal colors; web and mobile theme adapters resolve
those roles. Categorical chart palettes are likewise centralized in the theme
layer.

On web, `/branding/theme.css` remaps legacy non-neutral Tailwind palette names
to configured branding roles and exposes `data-1` through `data-7` for charts
and maps. Flutter theme owners expose the same brand-derived categorical order.
New slices use primary, accent, semantic, or data tokens directly. Structural
neutral surfaces, text, borders, shadows, white, and black remain in the shared
design-system foundation and are not tenant identity colors.

### Centralized Service Layer

Every feature follows a **three-tier pattern** where business logic lives once:

```
Core Service (app/services/{module}.py)
   ├── API Route (app/api/{module}.py)        → JSON responses
   └── Web Service (app/services/web_{module}.py) → template context
        └── Web Route (app/web/admin/{module}.py) → thin wrapper
```

**Rules:**
- Core services own ALL queries, mutations, validation, and stats computation
- Web services only transform core service output into template context dicts
- Routes (API and web) are thin wrappers — no `db.query()`, no conditionals, no aggregation
- Every dashboard stat method lives in the core service as `get_dashboard_stats(db) -> dict`
- API and web routes call the same `get_dashboard_stats()` — identical numbers everywhere

### Dashboard Stats Service Pattern

Every module that has a dashboard exposes a stats method in its core service:

```python
# app/services/{module}.py
class ModuleManager(ListResponseMixin):
    @staticmethod
    def get_dashboard_stats(db: Session, *, organization_id: str | None = None) -> dict:
        """Return all KPIs for this module's dashboard."""
        ...
        return {
            "kpi_cards": [...],
            "charts": {...},
            "tables": {...},
            "alerts": [...],
        }
```

This method is called by:
- `GET /api/v1/{module}/dashboard` → returns JSON
- `GET /admin/{module}` → web service wraps into template context

---

## Module-by-Module Specification

---

## 1. MAIN DASHBOARD (`/admin/dashboard`)

**Accent color:** slate (system)
**Purpose:** Single-screen operational health. Answer "is anything broken?" in 2 seconds.

### KPI Cards (top row, 4 cards)

| Card | Value | Trend | Link |
|------|-------|-------|------|
| Active Subscribers | count | % change vs last month | `/admin/subscribers?status=active` |
| MRR | currency | % change vs last month | `/admin/reports/revenue` |
| Open Service Orders | count | new today | `/admin/provisioning?status=pending` |
| Network Health | percentage | devices online / total | `/admin/network/monitoring` |

### Attention Required Panel (right sidebar, always visible)

Red/amber items that need operator action:
- Overdue invoices count + total amount → link to filtered invoice list
- Failed service orders count → link to filtered order list
- Offline devices count → link to monitoring with offline filter
- Expiring subscriptions (next 7 days) → link to filtered subscription list
- Failed webhook deliveries (last 24h) → link to webhook history
- Celery task failures (last 24h) → link to scheduler

### Charts Row (2 charts side-by-side)

**Left: Revenue Trend** (area chart, 30d default)
- Billed vs collected overlay
- Time range selector: 7d / 30d / 90d / YTD

**Right: Subscriber Growth** (line chart, 30d default)
- Net growth (new minus churned)
- Stacked: new signups (emerald) + cancellations (rose)

### Activity Feed (bottom left, 60% width)

Recent system events: payments received, subscribers created, orders completed, devices online/offline.
Each row: icon + description + relative timestamp + link to entity.

### Quick Actions (bottom right, 40% width)

Large icon buttons for frequent tasks:
- New Subscriber
- Record Payment
- Create Invoice
- Create Service Order

### Service: `app/services/dashboard.py`

```python
class DashboardStats:
    @staticmethod
    def get_stats(db: Session) -> dict:
        return {
            "subscribers": {"active": N, "trend": +X%},
            "mrr": {"value": Decimal, "trend": +X%},
            "open_orders": {"count": N, "new_today": N},
            "network_health": {"percentage": 98.5, "online": N, "total": N},
            "attention": {
                "overdue_invoices": {"count": N, "amount": Decimal},
                "failed_orders": N,
                "offline_devices": N,
                "expiring_subscriptions": N,
                "failed_webhooks": N,
                "task_failures": N,
            },
            "revenue_trend": [{"date": ..., "billed": ..., "collected": ...}, ...],
            "subscriber_growth": [{"date": ..., "new": ..., "churned": ...}, ...],
            "recent_activity": [...],
        }
```

---

## 2. SUBSCRIBERS MODULE (`/admin/subscribers`)

> **Target-state design, not a current route contract.** The production list is
> `/admin/customers`; `app.web.admin.subscribers` and the subscriber Playwright
> page object are backward-compatible facades. Current list behavior is governed
> by `docs/FRONTEND_SPEC.md` and `docs/SOT_RELATIONSHIP_MAP.md`. Implementing a
> distinct subscriber screen requires a new approved product boundary rather
> than copying the customer list.

**Accent color:** indigo
**Purpose:** Complete subscriber lifecycle management.

### 2a. Subscriber Dashboard (`/admin/subscribers`)

#### KPI Cards (4 cards)

| Card | Value | Trend | Color |
|------|-------|-------|-------|
| Total Active | count | % change 30d | emerald |
| New This Month | count | vs last month | blue |
| Suspended | count | change 30d | amber |
| Churn Rate | percentage | vs last month | rose |

#### Charts

**Left: Subscriber Status Breakdown** (doughnut)
- Active (emerald), Suspended (amber), Canceled (rose), Pending (blue)

**Right: Signup Trend** (bar chart, 12 months)
- Monthly new signups with trend line

#### Table: Recent Subscribers

Columns: Name, Account #, Plan, Status, Balance, Created, Actions
Default sort: newest first, limit 10
Link to full list with filters

### 2b. Subscriber List (`/admin/subscribers/list`)

#### Filters Bar
- **Search**: name, email, phone, account number, IP address (typeahead)
- **Status**: multi-select chips (active, suspended, canceled, pending)
- **Plan**: dropdown from catalog offers
- **Reseller**: dropdown (if multi-reseller)
- **Region**: dropdown from region zones
- **Balance**: owing / credit / zero
- **Date range**: created_at range picker

#### Table Columns
Name | Account # | Plan | Speed | Status | Balance | Last Payment | Actions

#### Bulk Actions (floating bar on row selection)
- Suspend selected
- Unsuspend selected
- Send notification
- Export selected (CSV)
- Change plan (if all same plan)

#### Actions per row (dropdown)
- View detail
- Edit
- Suspend / Unsuspend
- Create invoice
- Create service order
- Impersonate (customer portal)

### 2c. Subscriber Detail (`/admin/subscribers/{id}`)

#### Header
- Name, account number, status badge
- Quick action buttons: Edit, Suspend/Unsuspend, Create Invoice, Create Order

#### Tabbed Content

**Overview Tab:**
- Contact info card (name, email, phone, address)
- Account info card (account #, type, reseller, organization, created date)
- Current plan card (offer name, speed, price, billing cycle)
- Balance card (current balance, last payment date/amount)

**Services Tab:**
- Active subscriptions table (plan, status, start/end dates, monthly cost)
- RADIUS accounts table (username, profile, NAS, status)
- IP assignments table (address, pool, type, assigned date)
- CPE devices table (model, MAC, serial, status)

**Billing Tab:**
- Account balance summary card
- Recent invoices table (last 10) with link to full list
- Recent payments table (last 10)
- Credit notes table
- Payment arrangements (if any)
- Link: "View full ledger"

**Network Tab:**
- Connection status card (online/offline, last seen, uptime)
- Bandwidth usage chart (24h default, selectable: 7d/30d)
- Signal levels (for fiber: OLT/ONT signal, for wireless: RSSI)
- Session history table (RADIUS accounting sessions)

**Activity Tab:**
- Audit log filtered to this subscriber
- Status change history
- Communication history (emails, SMS sent)
- Support notes

**Documents Tab:**
- Contracts (signed/pending)
- Uploaded documents (ID, proof of address, etc.)
- Download/upload actions

### Service: `app/services/subscriber.py`

```python
class Subscribers(ListResponseMixin):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """KPIs for subscriber dashboard."""

    @staticmethod
    def get_detail_context(db: Session, subscriber_id: str) -> dict:
        """All data needed for subscriber detail page (all tabs)."""

    @staticmethod
    def search(db: Session, query: str, *, limit: int = 10) -> list[dict]:
        """Typeahead search across name, email, phone, account#, IP."""
```

---

## 3. BILLING MODULE (`/admin/billing`)

**Accent color:** emerald
**Purpose:** Complete financial lifecycle — invoicing, payments, collections, reporting.

### 3a. Billing Dashboard (`/admin/billing`)

#### KPI Cards (4 cards)

| Card | Value | Trend | Color |
|------|-------|-------|-------|
| Revenue This Month | currency | % vs last month | emerald |
| Outstanding | currency | change 30d | amber |
| Overdue | currency | change 30d | rose |
| Collection Rate | percentage | vs last month | blue |

#### Charts

**Left: Revenue Trend** (area chart, 6 months)
- Billed (line) vs Collected (filled area)
- Monthly comparison

**Right: Invoice Status Distribution** (doughnut)
- Paid (emerald), Pending (blue), Overdue (rose), Draft (slate), Void (gray)

#### AR Aging Summary (horizontal stacked bar)
- Current | 1-30 days | 31-60 days | 61-90 days | 90+ days
- Each segment shows amount and count
- Click segment → filtered invoice list

#### Quick Actions Grid (2x2)
- Generate Invoices (batch)
- Record Payment
- Issue Credit Note
- Run Dunning

#### Recent Payments Table (5 rows)
- Date, Subscriber, Amount, Channel, Status

### 3b. Invoices (`/admin/billing/invoices`)

#### Filters
- Status: draft, sent, paid, partially_paid, overdue, void
- Date range (issue date, due date)
- Subscriber (typeahead)
- Amount range (min/max)
- Reseller

#### Table Columns
Invoice # | Subscriber | Issue Date | Due Date | Amount | Paid | Balance | Status | Actions

#### Actions per row
- View, Edit (if draft), Send, Record Payment, Void, Download PDF

#### Batch Operations (toolbar)
- Generate batch invoices (select billing period, subscriber filter)
- Send batch (email all unsent)
- Export (CSV, PDF zip)

### 3c. Invoice Detail (`/admin/billing/invoices/{id}`)

#### Header
- Invoice # + status badge + issue/due dates
- Action buttons: Send, Record Payment, Void, Download PDF, Print

#### Content
- **Subscriber info** card (name, account #, address)
- **Line items table**: Description, Qty, Unit Price, Tax, Amount
- **Totals section**: Subtotal, Tax breakdown, Total, Paid, Balance Due
- **Payment allocations table**: Date, Payment #, Amount, Channel
- **Credit note applications**: Date, Credit Note #, Amount
- **Activity log**: Created, sent, payment received, voided — with timestamps

### 3d. Payments (`/admin/billing/payments`)

#### Filters
- Date range
- Status: pending, succeeded, failed, refunded
- Channel: card, bank_transfer, cash, check
- Allocation: allocated, unallocated, partially_allocated
- Subscriber (typeahead)

#### Table Columns
Payment # | Date | Subscriber | Amount | Channel | Status | Allocated | Actions

#### Unallocated Payments Alert
Banner at top: "X payments (total $Y) are unallocated" with link to filtered view

### 3e. Payment Detail (`/admin/billing/payments/{id}`)

- Payment info card (amount, date, channel, reference, status)
- Subscriber info card
- Allocation table (invoice #, amount allocated, date)
- "Allocate to Invoice" button → modal with open invoices
- Receipt download

### 3f. Credit Notes (`/admin/billing/credits`)

#### Table
Credit Note # | Date | Subscriber | Amount | Status | Applied | Actions

#### Credit Note Detail
- Line items, totals, application history
- "Apply to Invoice" action

### 3g. Accounts (`/admin/billing/accounts`)

#### Table
Account # | Subscriber | Balance | Status | Last Payment | Actions

#### Account Detail
- Balance summary (total billed, total paid, current balance)
- Full ledger (all transactions: invoices, payments, credits, adjustments)
- Aging breakdown for this account

### 3h. AR Aging Report (`/admin/billing/ar-aging`)

#### Filterable by reseller, region, plan

#### Table
Subscriber | Account # | Current | 1-30 | 31-60 | 61-90 | 90+ | Total

#### Summary row at bottom with totals per bucket

### 3i. Dunning (`/admin/billing/dunning`)

#### Dashboard Cards
- Active dunning cases | Resolved this month | Recovery rate | Avg days to resolve

#### Table
Subscriber | Amount Owed | Days Overdue | Dunning Step | Last Action | Next Action | Status

#### Dunning Detail
- Timeline of actions taken (emails, SMS, suspension warnings, suspension)
- Subscriber link, invoice links
- Manual action buttons: Send reminder, Suspend, Write off

### 3j. Tax Rates (`/admin/billing/tax-rates`)

CRUD table: Name, Code, Rate %, Active, Actions. Inclusive/exclusive/exempt is
selected on invoice or credit-note lines; it is not a property of the tax-rate
record.

### Tax Accounting Report (`/admin/reports/tax`)

- Date-filtered output tax recognized from issued invoice documents
- Issued credit-note tax adjustments and net output-tax liability per currency
- Credit-note periods use the persisted first-issuance tax point; automated
  cancellation credits retain the source line's configured tax treatment
- Totals separated by currency; never label issued tax as cash collected
- WHT gross settlement, net cash, total withheld, and outstanding receivable
- `app.services.tax_accounting` owns the projection; the route and template do
  not query invoices or reconstruct WHT status meaning
- The report remains the canonical source-document tax-register projection.
  Dotmac ERP separately owns configurable TaxCode account mappings, balanced
  journals, tax transactions, tax returns, and financial statements.
- `/admin/billing/tax-accounting` owns the Sub operator experience for the WHT
  evidence lifecycle. It provides server-side search, status filters, counts,
  newest-first ordering, and pagination; it has no account-mapping or posting
  controls. That queue may show direct-customer or consolidated-account WHT
  records and must render the correct customer/account identity rather than
  assuming every row belongs to a reseller.
- Invoice/credit-note tax treatment and payment WHT facts flow through bounded
  sync projections to the existing ERP pull integration. ERP fails closed when
  an effective account mapping is missing or ambiguous.
- Customer-facing automatic WHT is limited to invoice-linked direct bank
  transfer with an authoritative VAT-exclusive basis. Arbitrary account-credit
  deposits and online/card checkout remain full-value and do not ask the
  customer for a WHT rate.

### 3k. Payment Channels (`/admin/settings/billing/payment-channels`)

CRUD table: Name, Type, Provider, Active, Actions

#### Channel Detail
- Configuration form (provider-specific fields)
- Linked accounts table
- Transaction history

### Service: `app/services/billing/` package

```python
# In app/services/billing/reporting.py
class BillingReporting:
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """All billing dashboard KPIs, charts, tables."""

    @staticmethod
    def get_ar_aging(db: Session, *, organization_id: str | None = None) -> dict:
        """AR aging buckets with subscriber breakdown."""

    @staticmethod
    def get_revenue_trend(db: Session, *, months: int = 6) -> list[dict]:
        """Monthly billed vs collected."""
```

---

## 4. CATALOG MODULE (`/admin/catalog`)

**Accent color:** violet
**Purpose:** Service catalog management — plans, pricing, add-ons, policies.

### 4a. Catalog Dashboard (`/admin/catalog`)

#### KPI Cards (4 cards)

| Card | Value | Trend | Color |
|------|-------|-------|-------|
| Active Offers | count | new this month | violet |
| Total Subscriptions | count | active | emerald |
| ARPU | currency | vs last month | blue |
| Plan Changes | count | this month | amber |

#### Charts

**Left: Subscriptions by Plan** (horizontal bar chart)
- Each offer with active subscription count
- Sorted by popularity (most subscriptions first)

**Right: Revenue by Plan** (doughnut)
- Monthly revenue contribution per offer

#### Popular Plans Table (top 5)
Plan Name | Speed | Price | Subscribers | Revenue | Growth

### 4b. Offers (`/admin/catalog/offers`)

#### View Toggle: Card Grid (default) | Table

#### Card Grid
Each card shows:
- Plan name (heading)
- Speed tier badge (e.g., "100 Mbps")
- Price with billing cycle (e.g., "$49.99/mo")
- Active subscribers count badge
- Service type icon (fiber, wireless, DSL)
- Status indicator (active/draft/archived)
- Actions: Edit, Duplicate, Archive

#### Table View
Name | Service Type | Speed | Price | Billing Cycle | Subscribers | Status | Actions

#### Filters
- Service type: fiber, wireless, DSL, fixed_wireless
- Status: active, draft, archived
- Speed range
- Price range

### 4c. Offer Detail (`/admin/catalog/offers/{id}`)

#### Header
- Plan name + status badge
- Action buttons: Edit, Duplicate, Archive, Delete (if no subscribers)

#### Sections

**Pricing:**
- Base price, setup fee, billing cycle
- Tax configuration
- Version history (if versioned pricing)

**Specifications:**
- Download/upload speed
- Data cap / unlimited
- FUP thresholds and actions
- Contention ratio

**Add-ons:**
- Available add-ons table (name, price, description)
- Attach/detach add-ons

**RADIUS Profile:**
- Linked RADIUS profile name
- Attributes table (attribute, value, operator)
- Test profile button

**Subscribers:**
- Count of active subscribers on this plan
- Link to filtered subscriber list
- Revenue contribution stat

**Policies:**
- Linked policy set (proration, suspension, dunning rules)
- SLA profile (if assigned)

### 4d. Subscriptions (`/admin/catalog/subscriptions`)

#### Filters
- Status: active, suspended, pending, expired, canceled
- Offer (dropdown)
- Subscriber (typeahead)
- Expiry: expiring within 7/30/90 days

#### Table
Subscriber | Offer | Status | Start Date | End Date | Monthly Cost | Actions

#### Actions
- View detail, Change plan, Suspend, Cancel, Extend

### 4e. Add-ons (`/admin/catalog/add-ons`)

CRUD table: Name, Price, Type (one-time/recurring), Compatible Plans, Active, Actions

### 4f. Catalog Settings (sub-pages under `/admin/catalog/settings`)

**Billing Profiles:**
- Name, billing cycle, invoice day, due days, proration method

**Policy Sets:**
- Name, dunning steps configuration, suspension rules, grace periods

**SLA Profiles:**
- Name, uptime guarantee %, response time, resolution time

**Region Zones:**
- Name, regions included, default pricing multiplier

**Usage Allowances:**
- Name, data cap, FUP threshold, throttle speed, action

### Service: `app/services/catalog/` package

```python
class Offers(CRUDManager[CatalogOffer]):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Catalog dashboard KPIs."""

    @staticmethod
    def get_popularity_ranking(db: Session, *, limit: int = 10) -> list[dict]:
        """Plans ranked by active subscription count."""

    @staticmethod
    def get_revenue_contribution(db: Session) -> list[dict]:
        """Revenue breakdown by plan."""
```

---

## 5. PROVISIONING MODULE (`/admin/provisioning`)

**Accent color:** amber
**Purpose:** Service order lifecycle — from request to activation.

### 5a. Provisioning Dashboard (`/admin/provisioning`)

#### KPI Cards (4 cards)

| Card | Value | Trend | Color |
|------|-------|-------|-------|
| Open Orders | count | new today | amber |
| In Progress | count | assigned | blue |
| Completed Today | count | vs yesterday | emerald |
| Failed | count | last 7 days | rose |

#### Workflow Funnel (horizontal funnel chart)
Draft → Submitted → Scheduled → In Progress → Completed
Each stage shows count and drop-off rate

#### Charts

**Left: Orders by Type** (doughnut)
- New Install, Plan Change, Relocation, Disconnection

**Right: Completion Trend** (bar chart, 30 days)
- Daily completed orders with average line

#### Metrics Cards (secondary row)
- Avg Time to Complete (days/hours)
- First-Visit Success Rate (%)
- Overdue Orders (scheduled but not started)
- Pending Appointments

### 5b. Service Orders List (`/admin/provisioning/orders`)

#### View Toggle: Table (default) | Kanban Board

#### Kanban Board
Columns: Draft | Submitted | Scheduled | In Progress | Completed | Failed
Cards show: Subscriber name, order type, priority indicator, assigned tech, due date
Drag-and-drop between columns (updates status)

#### Table Filters
- Status: draft, submitted, scheduled, in_progress, completed, failed, canceled
- Type: new_install, plan_change, relocation, disconnection
- Priority: low, normal, high, urgent
- Assigned to (technician dropdown)
- Date range (created, scheduled)

#### Table Columns
Order # | Subscriber | Type | Priority | Status | Scheduled | Assigned | Actions

### 5c. Service Order Detail (`/admin/provisioning/orders/{id}`)

#### Header
- Order # + type badge + priority indicator + status badge
- Action buttons: Assign, Schedule, Start, Complete, Cancel

#### Layout: 2-column

**Left column (60%):**

**Workflow Timeline** (vertical stepper)
- Each step: name, status icon, timestamp, duration, assigned user
- Current step highlighted with pulsing indicator
- Failed steps show error message

**Tasks Checklist:**
- Equipment delivery
- CPE installation
- Cable termination
- NAS provisioning
- RADIUS account creation
- Speed test verification
- Customer sign-off

**Notes & Activity:**
- Comment thread (operator notes, system events)
- File attachments (site photos, test results)

**Right column (40%):**

**Subscriber Card:**
- Name, address, contact phone, plan ordered

**Appointment Card:**
- Scheduled date/time, technician, status
- Reschedule button

**Equipment Card:**
- Assigned CPE (model, serial, MAC)
- ONT (if fiber)
- Router/access point

**Network Card:**
- IP assignment
- VLAN assignment
- RADIUS account status
- NAS device

### 5d. Workflows (`/admin/provisioning/workflows`)

#### Table
Name | Steps | Order Types | Active | Last Used | Actions

#### Workflow Editor
- Step list (drag to reorder)
- Each step: name, type (manual/auto), timeout, required
- Conditions for step transitions
- Notification triggers per step

### 5e. Appointments (`/admin/provisioning/appointments`)

#### Calendar View (default) | Table View

#### Calendar
- Month/week/day views
- Color-coded by order type
- Click to view/edit appointment
- Drag to reschedule

#### Table
Date | Time | Subscriber | Address | Technician | Order Type | Status | Actions

### Service: `app/services/provisioning.py`

```python
class ServiceOrders(CRUDManager[ServiceOrder]):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Provisioning dashboard KPIs and funnel data."""

    @staticmethod
    def get_workflow_funnel(db: Session, *, days: int = 30) -> dict:
        """Order counts by stage with completion rates."""

    @staticmethod
    def get_completion_metrics(db: Session) -> dict:
        """Avg time to complete, first-visit rate, etc."""

    @staticmethod
    def get_kanban_board(db: Session) -> dict[str, list]:
        """Orders grouped by status for kanban view."""
```

---

## 6. NETWORK MODULE (`/admin/network`)

**Accent color:** blue
**Purpose:** Infrastructure visibility and management.

### 6a. Network Dashboard (`/admin/network`)

#### KPI Cards (4 cards)

| Card | Value | Trend | Color |
|------|-------|-------|-------|
| Devices Online | count / total | % uptime | emerald |
| Active Alarms | count | critical / major / minor | rose |
| IP Utilization | percentage | pools near capacity | amber |
| Bandwidth | Gbps | peak today | blue |

#### Network Health Map (main visual)
- Interactive topology map showing device status (green/amber/red)
- Click device → popover with: name, type, uptime, active ports, alarms
- Click through to device detail

#### Charts

**Left: Device Status Distribution** (doughnut)
- Online (emerald), Degraded (amber), Offline (rose), Maintenance (slate)

**Right: Bandwidth Utilization** (area chart, 24h)
- Aggregate bandwidth in/out with peak markers

#### Active Alarms Table (5 rows, critical first)
Time | Device | Severity | Message | Acknowledged | Actions

#### Capacity Warnings
- IP pools > 80% utilized
- OLT ports > 90% utilized
- Fiber strands near capacity

### 6b. Devices (`/admin/network/devices`)

#### Sub-navigation tabs: All | OLTs | ONTs | CPEs | Core | NAS

#### Filters (shared)
- Status: online, offline, degraded, maintenance
- Type: OLT, ONT, CPE, router, switch
- Site/POP
- Model/vendor

#### Table Columns
Name | Type | IP | Model | Status | Uptime | Ports | Alarms | Actions

### 6c. OLT Detail (`/admin/network/olts/{id}`)

#### Header
- Device name + model + status badge + uptime
- Actions: Reboot, Backup Config, Edit, Maintenance Mode

#### Tabs

**Overview:**
- Device info card (model, firmware, serial, location)
- Port utilization summary (used/total per card)
- Uptime graph (30 days)

**PON Ports:**
- Table: Port # | ONTs | Utilization % | Rx Power | Tx Power | Status
- Click port → ONT list for that port

**ONTs:**
- Table of all connected ONTs
- Columns: Serial, Subscriber, Port, Signal (dBm with color), Status, Uptime

**Alarms:**
- Recent alarms for this device
- Severity, message, time, acknowledged status

**Config:**
- Current running config (read-only, syntax highlighted)
- Backup history table

**Metrics:**
- CPU, memory, temperature charts (24h)
- Interface traffic charts

### 6d. ONT Detail (`/admin/network/onts/{id}`)

- ONT info (serial, model, firmware, OLT/port assignment)
- Subscriber link
- Signal levels with thresholds (green/yellow/red indicators)
- Traffic charts (in/out, 24h/7d)
- Uptime history
- Provisioning status

### 6e. IP Management (`/admin/network/ip-management`)

#### Dashboard Cards
- Total IPs | Used | Available | Reserved | Utilization %

#### Pool List Table
Pool Name | CIDR | Type (v4/v6) | Used/Total | Utilization Bar | Status | Actions

#### Utilization bars color-coded:
- < 70% green, 70-85% amber, > 85% rose

#### Pool Detail
- Block allocation table
- IP assignment table (IP, Subscriber, MAC, Assigned Date, Status)
- Available IPs count
- Reserve/release actions

### 6f. VLANs (`/admin/network/vlans`)

#### Table
VLAN ID | Name | Description | Subscribers | Ports | Status | Actions

### 6g. Fiber Plant (`/admin/network/fiber-plant`)

#### Sub-tabs: FDH Cabinets | Fiber Strands | Splice Closures | POP Sites

#### FDH Detail
- Cabinet info (location, capacity)
- Splitter ports table (port, strand, subscriber, status)
- Utilization gauge

### 6h. NAS Devices (`/admin/nas`)

#### Table
Name | IP | Vendor | Type | Status | Subscribers | Last Backup | Actions

#### NAS Detail
- Connection info (IP, port, vendor, model)
- Credentials (masked, reveal toggle, encrypted indicator)
- RADIUS clients configured
- Subscriber count
- Config backup history with download
- Template assignments
- Test connection button

### 6i. Monitoring (`/admin/network/monitoring`)

#### Device Grid View
- Cards in grid (3-4 per row)
- Each card: device name, status indicator, key metric (CPU/bandwidth), mini sparkline
- Click → device detail

#### Alarm Management
- Active alarms table with severity filtering
- Acknowledge, resolve, escalate actions
- Alarm rules configuration (thresholds, notification targets)

### Service: `app/services/network/` package + `app/services/network_monitoring.py`

```python
class NetworkDevices(ListResponseMixin):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Network health, device counts, alarm summary, bandwidth."""

    @staticmethod
    def get_device_health_grid(db: Session) -> list[dict]:
        """All devices with status/metrics for grid view."""

    @staticmethod
    def get_capacity_warnings(db: Session) -> list[dict]:
        """Resources approaching capacity limits."""
```

---

## 7. REPORTS & ANALYTICS (`/admin/reports`)

**Accent color:** teal
**Purpose:** Operational intelligence and trend analysis.

### 7a. Reports Dashboard (`/admin/reports`)

Overview page with links to all reports, each showing a preview stat:

| Report | Preview Stat |
|--------|-------------|
| Revenue | MRR this month |
| Subscribers | Net growth this month |
| Churn | Churn rate this month |
| Network | Avg uptime % |
| Usage | Avg data consumed |
| Technician | Jobs completed this month |
| Collections | Recovery rate |

### 7b. Revenue Report (`/admin/reports/revenue`)

#### Filters: Date range, Reseller, Region, Plan

#### KPI Cards
- Total Revenue | MRR | ARR | ARPU | Collection Rate

#### Charts
- Revenue trend (line, monthly)
- Revenue by plan (stacked bar)
- Revenue by payment channel (doughnut)
- Revenue by reseller (horizontal bar) — if multi-reseller

#### Tables
- Top 10 subscribers by revenue
- Revenue breakdown by plan (plan, subscribers, revenue, % of total)
- Monthly summary table (exportable CSV/PDF)

### 7c. Subscriber Report (`/admin/reports/subscribers`)

#### Filters: Date range, Status, Plan, Reseller, Region

#### KPI Cards
- Total Subscribers | New This Period | Net Growth | Growth Rate %

#### Charts
- Growth trend (line chart: new vs churned vs net)
- Status distribution (doughnut)
- Subscribers by plan (horizontal bar)
- Subscribers by region (map or bar)

#### Tables
- Monthly growth summary
- New subscribers list (with source/channel if tracked)

### 7d. Churn Report (`/admin/reports/churn`)

#### KPI Cards
- Churn Rate | Retention Rate | Avg Customer Lifetime | Churned This Month

#### Charts
- Churn rate trend (line, monthly)
- Churn by reason (bar chart: price, service quality, moved, competitor, other)
- Churn by plan (identifies high-churn plans)
- Churn by tenure (how long before they cancel)

#### Tables
- Recent cancellations (subscriber, plan, reason, tenure, date)
- At-risk subscribers (late payments, complaints, low usage)

### 7e. Network Usage Report (`/admin/reports/network`)

#### KPI Cards
- Avg Bandwidth | Peak Bandwidth | Total Data Transferred | Avg Usage per Subscriber

#### Charts
- Bandwidth utilization trend (area chart, 30d)
- Usage by plan tier (bar chart)
- Top consumers (horizontal bar, top 10)
- Peak hours heatmap (hour of day × day of week)

#### Tables
- Top 20 consumers (subscriber, plan, usage GB, % of allowance)
- Per-plan usage summary

### 7f. Technician Report (`/admin/reports/technician`)

#### KPI Cards
- Total Technicians | Jobs Completed | Avg Completion Time | First-Visit Rate

#### Charts
- Jobs by technician (bar chart)
- Completion time distribution (histogram)
- Job type breakdown (doughnut)

#### Tables
- Technician leaderboard (name, jobs, avg time, first-visit %, rating)
- Recent job completions

### 7g. Collections Report (`/admin/reports/collections`)

#### KPI Cards
- Total Outstanding | In Collections | Recovery Rate | Avg Days to Resolve

#### Charts
- AR aging trend (stacked area, monthly)
- Recovery by dunning step (funnel)
- Collections by status (doughnut)

#### Tables
- Top debtors (subscriber, amount, days overdue, dunning step)
- Monthly collection summary

### Shared Report Patterns

Every report page has:
- Date range selector (7d / 30d / 90d / YTD / Custom)
- Reseller/organization filter (if multi-tenant)
- Export buttons: CSV, PDF
- Print-friendly view

### Service: `app/services/reports.py`

```python
class ReportingService:
    @staticmethod
    def revenue_report(db: Session, *, start: date, end: date, **filters) -> dict:
        """Full revenue report data."""

    @staticmethod
    def subscriber_report(db: Session, *, start: date, end: date, **filters) -> dict:
        """Full subscriber report data."""

    @staticmethod
    def churn_report(db: Session, ...) -> dict: ...
    @staticmethod
    def network_report(db: Session, ...) -> dict: ...
    @staticmethod
    def technician_report(db: Session, ...) -> dict: ...
    @staticmethod
    def collections_report(db: Session, ...) -> dict: ...
```

---

## 8. SYSTEM MODULE (`/admin/system`)

**Accent color:** slate
**Purpose:** Platform administration, RBAC, audit, health.

### 8a. System Dashboard (`/admin/system`)

#### KPI Cards (4 cards)

| Card | Value | Color |
|------|-------|-------|
| Admin Users | count (active/total) | slate |
| Roles Defined | count | blue |
| API Keys | active count | violet |
| System Health | status indicator | emerald/rose |

#### System Status Grid
- Database: status, connections, size
- Redis: status, memory, keys
- Celery: workers online, tasks queued, failed
- SMTP: last test status
- Disk: usage percentage

#### Recent Audit Events (5 rows)
Actor | Action | Entity | Timestamp

#### Quick Links
- Users, Roles, API Keys, Webhooks, Settings, Health

### 8b. Users (`/admin/system/users`)

#### Table
Name | Email | Role | Status | Last Login | MFA | Actions

#### User Form
- Name, email, password, role selection, MFA setup
- Organization assignment (if multi-tenant)
- Permission overrides

### 8c. Roles & Permissions (`/admin/system/roles`)

#### Roles Table
Role Name | Users | Permissions | Actions

#### Role Detail / Editor
- Role name, description
- Permission matrix: Resource × Action checkboxes
- Resources: subscribers, billing, catalog, network, provisioning, system, reports
- Actions: read, write, delete, admin

### 8d. API Keys (`/admin/system/api-keys`)

#### Table
Name | Key (masked) | Permissions | Created | Last Used | Status | Actions

#### Create Form
- Name, description, permission scope, expiry date
- Show key once on creation (copy button, never shown again)

### 8e. Webhooks (`/admin/system/webhooks`)

#### Table
URL | Events | Status | Success Rate | Last Delivery | Actions

#### Webhook Form
- URL, secret, events (multi-select checkboxes)
- Test webhook button

#### Webhook Detail
- Configuration
- Delivery history table (timestamp, event, status, response code, retry count)
- Retry failed button

### 8f. Audit Log (`/admin/system/audit`)

#### Filters
- Actor (user dropdown)
- Action type
- Entity type
- Date range

#### Table
Timestamp | Actor | Action | Entity Type | Entity ID | Details (expandable)

### 8g. Health (`/admin/system/health`)

#### Service Status Cards
Each card: service name, status badge, response time, last check

Services monitored:
- PostgreSQL (connections, query latency, db size)
- Redis (memory, connections, hit rate)
- Celery (workers, queue depth, failed tasks)
- SMTP (connectivity test result)
- RADIUS (server reachability)
- External APIs (payment gateway, SMS provider)

#### System Metrics
- CPU usage gauge
- Memory usage gauge
- Disk usage gauge
- Load average

#### Recent Errors
- Last 10 application errors from logs

### 8h. Scheduler (`/admin/system/scheduler`)

#### Table
Task Name | Schedule | Last Run | Next Run | Status | Duration | Actions

#### Task Detail
- Task configuration (interval, parameters)
- Run history table (start, end, duration, status, result stats)
- Manual run button
- Enable/disable toggle

### Service: `app/services/system.py`

```python
class SystemStats:
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """System health, user counts, recent audit events."""

    @staticmethod
    def get_health_status(db: Session) -> dict:
        """All service health checks."""
```

---

## 9. NOTIFICATIONS MODULE (`/admin/notifications`)

**Accent color:** rose
**Purpose:** Notification management, templates, delivery tracking.

### 9a. Notifications Dashboard (`/admin/notifications`)

#### KPI Cards (4 cards)

| Card | Value | Color |
|------|-------|-------|
| Sent Today | count | blue |
| Delivery Rate | percentage | emerald |
| Failed | count (24h) | rose |
| Queued | count | amber |

#### Charts

**Left: Delivery by Channel** (doughnut)
- Email, SMS, In-App, WhatsApp, Push

**Right: Delivery Trend** (bar chart, 7 days)
- Daily sent count with success/fail stacking

#### Recent Failures Table (5 rows)
Time | Recipient | Channel | Error | Actions (retry)

### 9b. Templates (`/admin/notifications/templates`)

#### Table
Name | Channel | Event Trigger | Variables | Status | Actions

#### Template Editor
- Name, channel (email/sms/push/whatsapp)
- Subject (for email)
- Body with variable insertion ({{ subscriber_name }}, {{ invoice_amount }}, etc.)
- Preview button (renders with sample data)
- Test send button (to current admin)

### 9c. Queue (`/admin/notifications/queue`)

#### Filters: Status, Channel, Date range

#### Table
Time | Recipient | Channel | Subject/Preview | Status | Retry Count | Actions

### 9d. History (`/admin/notifications/history`)

#### Filters: Channel, Status, Date range, Recipient

#### Table
Time | Recipient | Channel | Subject | Status | Delivered At | Actions (view detail)

### 9e. Alert Policies (`/admin/notifications/policies`)

#### Table
Name | Trigger Event | Escalation Steps | On-Call Rotation | Active | Actions

#### Policy Editor
- Trigger: event type selection
- Escalation steps: ordered list with delay between steps
- Each step: channel, recipients (users/roles/on-call), template
- On-call rotation assignment

### 9f. On-Call Rotations (`/admin/notifications/on-call`)

#### Table
Name | Members | Schedule | Current On-Call | Actions

#### Rotation Editor
- Name, schedule type (daily/weekly/custom)
- Members list (drag to reorder)
- Override calendar

### Service: `app/services/notification.py`

```python
class Notifications(ListResponseMixin):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Notification KPIs, delivery rates, channel breakdown."""

    @staticmethod
    def get_delivery_metrics(db: Session, *, days: int = 7) -> dict:
        """Delivery success/fail rates by channel over time."""
```

---

## 10. INTEGRATIONS MODULE (`/admin/integrations`)

**Accent color:** cyan
**Purpose:** External system connections, sync jobs, connector management.

### 10a. Integrations Dashboard (`/admin/integrations`)

#### KPI Cards (4 cards)

| Card | Value | Color |
|------|-------|-------|
| Active Connectors | count / total | emerald |
| Sync Jobs | running / total | blue |
| Failed Jobs (24h) | count | rose |
| Webhook Deliveries (24h) | success rate % | amber |

#### Connector Status Grid
Cards for each configured connector:
- Name, type icon, status (connected/error/disabled)
- Last sync time
- Error indicator if failing
- Click → connector detail

#### Recent Job Runs Table (5 rows)
Job | Connector | Status | Duration | Records | Errors

### 10b. Connectors (`/admin/integrations/connectors`)

#### Card Grid
Each card:
- Connector name + type icon
- Status badge (connected, error, disabled)
- Base URL (truncated)
- Last successful sync
- Actions: Edit, Test, Disable/Enable

#### Connector Form
- Name, type (dropdown: webhook, http, smtp, stripe, twilio, etc.)
- Base URL
- Auth type (none, basic, bearer, api_key, oauth2, hmac)
- Auth config (dynamic fields based on auth type):
  - Basic: username, password
  - Bearer: token
  - API Key: key name, key value, header/query
  - OAuth2: client_id, client_secret, token_url, scopes
  - HMAC: secret, algorithm
- Custom headers (key-value pairs)
- Timeout, retry policy
- Test connection button

### 10c. Integration Jobs (`/admin/integrations/jobs`)

#### Table
Job Name | Connector | Type | Schedule | Last Run | Status | Actions

#### Job Form
- Name, connector (dropdown)
- Type: sync, export, import
- Target: RADIUS, CRM, billing, custom
- Schedule: manual, interval (minutes)
- Configuration (JSON editor or structured form)
- Enable/disable toggle

#### Job Detail
- Configuration summary
- Run history table (start, end, duration, status, records processed, errors)
- Error log for failed runs
- Manual run button

### 10d. Webhook Endpoints (`/admin/integrations/webhooks`)

(Moved from System — centralizes all external communication)

#### Table
URL | Events | Status | Success Rate | Last Delivery | Actions

### Service: `app/services/integration.py` + `app/services/connector.py`

```python
class IntegrationJobs(ListResponseMixin):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Integration health, connector status, job metrics."""

class ConnectorConfigs(ListResponseMixin):
    @staticmethod
    def test_connection(db: Session, connector_id: str) -> dict:
        """Test connector reachability and auth."""
```

---

## 11. RADIUS SETTINGS (`/admin/settings/radius`)

**Accent color:** blue
**Parent:** Settings section or Network module

### 11a. RADIUS Dashboard

#### Status Cards
- Primary Server: status, response time, last sync
- Secondary Server: status (if configured)
- Sync Status: last run, next scheduled, success/fail counts

#### KPI Cards
- RADIUS Users | Active Sessions | Auth Requests (24h) | Auth Failures (24h)

### 11b. Servers (`/admin/settings/radius/servers`)

#### Table
Name | IP:Port | Type (auth/acct) | Status | Response Time | Actions

#### Server Form
- Name, IP address, auth port (1812), acct port (1813)
- Shared secret (encrypted, masked)
- Timeout, retries
- Dictionary path
- CoA settings (enabled, port, timeout, retries)
- Test connection button

### 11c. Profiles (`/admin/settings/radius/profiles`)

#### Table
Name | Speed (Down/Up) | Attributes | Linked Offers | Actions

#### Profile Form
- Name, description
- Bandwidth: download speed, upload speed (with unit selector: Kbps/Mbps/Gbps)
- Address pool assignment
- Custom attributes table (attribute name, value, operator: =, :=, +=, ==)
- Linked catalog offers (read-only, shows which plans use this profile)

### 11d. Sync Jobs (`/admin/settings/radius/sync`)

#### Table
Name | Type | Schedule | Last Run | Status | Actions

#### Sync Configuration
- Sync users: on/off, interval
- Sync NAS clients: on/off, interval
- Sync profiles: on/off, interval
- Force full sync button
- Run history with logs

### 11e. Clients (NAS Devices as RADIUS Clients)

#### Table
NAS Name | IP | Shared Secret (masked) | Status | Actions

Linked from NAS device management — shows RADIUS-specific view

### Service: `app/services/radius.py`

```python
class RadiusServers(ListResponseMixin):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """RADIUS health, session counts, auth metrics."""

    @staticmethod
    def test_connectivity(db: Session, server_id: str) -> dict:
        """Test RADIUS server reachability."""
```

---

## 12. USAGE & FUP (`/admin/catalog/usage`)

**Accent color:** violet (catalog sub-module)

### 12a. Usage Dashboard

#### KPI Cards
- Total Data (this month) | Avg per Subscriber | Over-Quota Subscribers | FUP Actions Taken

#### Charts
**Left: Usage Distribution** (histogram)
- Subscriber count by usage bucket (0-10GB, 10-50GB, 50-100GB, 100GB+)

**Right: Usage Trend** (line chart, 30d)
- Daily aggregate usage

#### Over-Quota Subscribers Table (needs attention)
Subscriber | Plan | Allowance | Used | % | FUP Action | Actions

### 12b. Usage Records (`/admin/catalog/usage/records`)

#### Filters: Subscriber, Date range, Plan

#### Table
Subscriber | Date | Download | Upload | Total | Source | Actions

### 12c. Usage Rating Runs (`/admin/catalog/usage/runs`)

#### Table
Run ID | Start | End | Records Processed | Charges Created | Errors | Status

### 12d. FUP Calculator (`/admin/catalog/usage/calculator`)

Interactive tool:
- Select a plan → shows allowance, thresholds
- Input usage amount → shows what FUP action would apply
- Preview throttle speed, notification triggers

### Service: `app/services/usage.py`

```python
class UsageRecords(ListResponseMixin):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Usage KPIs, distribution, over-quota alerts."""

    @staticmethod
    def get_usage_distribution(db: Session) -> list[dict]:
        """Subscriber counts by usage bucket."""
```

---

## 13. COLLECTIONS & DUNNING (`/admin/billing/collections`)

**Accent color:** emerald (billing sub-module)

### 13a. Collections Dashboard

#### KPI Cards
- Total in Collections | Recovery Rate | Avg Resolution Days | Active Dunning Cases

#### Dunning Funnel (horizontal funnel)
Reminder 1 → Reminder 2 → Warning → Suspension → Write-off
Each step: count, amount, success rate

#### Charts
**Left: Recovery Trend** (line chart, 6 months)
**Right: Collections by Status** (doughnut: open, paused, resolved, closed)

### 13b. Dunning Cases (`/admin/billing/collections/dunning`)

#### Table
Subscriber | Amount | Days Overdue | Step | Last Action | Next Action | Status | Actions

#### Case Detail
- Subscriber info, outstanding invoices
- Dunning timeline (each step with date, action, result)
- Manual action buttons: Send Reminder, Schedule Callback, Suspend, Write Off, Resolve
- Notes/comments thread

### 13c. Prepaid Enforcement (`/admin/billing/collections/prepaid`)

#### KPI Cards
- Prepaid Subscribers | Below Minimum | Warned | Suspended

#### Table
Subscriber | Balance | Min Required | Status | Last Warning | Actions

### 13d. Collection Settings

- Dunning steps configuration (step name, delay days, action, template)
- Prepaid thresholds (minimum balance, grace days, warning template)
- Throttle RADIUS profile for throttled subscribers
- Shared daily enforcement start/end time in the scheduler timezone

### Service: `app/services/collections/`

```python
class CollectionsStats:
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Collections KPIs, funnel data, recovery metrics."""
```

---

## 14. AUTOMATION (`/admin/system/automation`)

**Accent color:** slate
**Purpose:** Celery task management, scheduled jobs, billing automation.

### 14a. Automation Dashboard

#### KPI Cards
- Scheduled Tasks | Running Now | Completed Today | Failed Today

#### Task Status Grid
Cards for each major automation:
- Billing Cycle (last run, next run, invoices generated)
- Dunning Run (last run, cases processed)
- Usage Rating (last run, records rated)
- RADIUS Sync (last run, users synced)
- GIS Sync (last run, records updated)
- Prepaid Enforcement (last run, actions taken)
- Notification Queue (last run, sent/failed)
- Bandwidth Aggregation (last run, samples processed)

Each card shows: status indicator, last run time, next scheduled, success/fail

#### Recent Task Runs Table
Task | Started | Duration | Status | Result Stats | Actions (view log)

### 14b. Scheduled Tasks (`/admin/system/automation/tasks`)

#### Table
Task Name | Module | Interval | Enabled | Last Run | Next Run | Status | Actions

#### Task Configuration
- Name (read-only, from code)
- Interval (editable: seconds/minutes/hours/days)
- Enabled toggle
- Configuration parameters (task-specific)
- Run history
- Manual run button

### 14c. Billing Automation (`/admin/system/automation/billing`)

Dedicated page for billing cycle configuration:

**Invoice Generation:**
- Enabled/disabled toggle
- Run interval
- Invoice due days (default: 14)
- Auto-send on generation (on/off)
- Invoice number format (prefix, padding, start number)

**Proration:**
- Enabled/disabled
- Proration method (daily/none)

**Auto-activation:**
- Auto-activate pending subscriptions on billing (on/off)

**Credit Note Numbering:**
- Prefix, padding, start number

**Preview:** "Next billing run will generate ~X invoices for $Y total"

### 14d. Task Run History (`/admin/system/automation/history`)

#### Filters: Task, Status, Date range

#### Table
Task | Run ID | Started | Ended | Duration | Status | Processed | Errors | Actions

#### Run Detail
- Full execution log
- Statistics (records processed, created, updated, errors)
- Error details (if failed)

### Service: `app/services/scheduler.py` + `app/services/billing_automation.py`

```python
class ScheduledTasks(ListResponseMixin):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Automation health, task status grid, recent runs."""

    @staticmethod
    def get_task_status_grid(db: Session) -> list[dict]:
        """Status of each major scheduled task."""
```

---

## 15. GIS / MAPPING (`/admin/gis`)

**Accent color:** green
**Purpose:** Geographic visualization and coverage management.

### 15a. GIS Dashboard

#### KPI Cards
- Mapped Subscribers | Coverage Areas | POP Sites | Unmapped Subscribers

#### Map (main visual, full-width)
- Leaflet/MapLibre map
- Layer toggles: Subscribers, POP Sites, Fiber Routes, Coverage Areas, FDH Cabinets
- Clustering for dense subscriber areas
- Click subscriber pin → popover (name, plan, status, signal)
- Click POP → popover (name, capacity, connected subscribers)

#### Coverage Stats
- Coverage area (sq km)
- Subscribers per sq km density
- Coverage gaps identified

### 15b. Locations (`/admin/gis/locations`)

#### Table
Subscriber | Address | Coordinates | Accuracy | Source | Actions

#### Location Form
- Address fields (street, city, state, postal)
- Map picker (click to set coordinates)
- Geocode button (auto-fill from address)

### 15c. Coverage Areas (`/admin/gis/coverage`)

#### Table
Area Name | Type | Subscribers | Size | Status | Actions

#### Area Editor
- Name, type (serviceable, planned, under construction)
- Polygon drawing on map
- Assign to region zone

### 15d. GIS Sync Settings (`/admin/gis/settings`)

- Sync enabled/disabled
- Sync interval
- Data sources (POP sites, addresses)
- Deactivate missing records on/off

### Service: `app/services/gis.py`

```python
class GeoLocations(ListResponseMixin):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """GIS KPIs, coverage stats, unmapped count."""

    @staticmethod
    def get_map_data(db: Session, *, bounds: dict | None = None) -> dict:
        """All layers for map rendering within bounds."""
```

---

## 16. VPN / WIREGUARD (`/admin/vpn`)

**Accent color:** cyan
**Purpose:** VPN service management for subscriber tunnels.

### 16a. VPN Dashboard

#### KPI Cards
- VPN Servers | Active Peers | Connected Now | Bandwidth (24h)

#### Charts
**Left: Connection Trend** (area chart, 7d)
- Concurrent connections over time

**Right: Bandwidth by Server** (bar chart)
- Per-server bandwidth usage

#### Active Peers Table (top 10 by traffic)
Peer | Server | IP | Connected Since | Traffic (In/Out) | Handshake

### 16b. Servers (`/admin/vpn/servers`)

#### Table
Name | Endpoint | Port | Interface | Peers | Status | Actions

#### Server Form
- Name, listen port, interface name
- VPN address (IPv4 subnet)
- VPN address v6 (optional)
- MTU, DNS servers
- Public key (auto-generated, read-only)
- Private key (encrypted, never displayed)
- MikroTik router sync (router IP, credentials)

### 16c. Peers (`/admin/vpn/peers`)

#### Table
Name | Server | Allowed IPs | Last Handshake | Traffic | Status | Actions

#### Peer Form
- Name, server assignment
- Subscriber link (optional)
- Allowed IPs
- DNS override
- Persistent keepalive
- QR code generation for mobile config
- Config file download (.conf)

### 16d. Peer Detail

- Connection info card
- Traffic chart (24h/7d)
- Handshake history
- Configuration display (masked private key)

### 16e. Connection Logs (`/admin/vpn/logs`)

#### Table
Time | Peer | Event (connect/disconnect) | Duration | Traffic | IP

### Service: `app/services/wireguard.py`

```python
class WireGuardServerService:
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """VPN KPIs, connection trends, bandwidth."""
```

---

## 17. SETTINGS HUB (`/admin/settings`)

**Accent color:** slate
**Purpose:** Centralized configuration for all system settings.

### 17a. Settings Dashboard

Organized by domain with cards:

| Domain | Description | Settings Count |
|--------|-------------|---------------|
| Billing | Currency, numbering, automation | 15+ |
| Catalog | Proration, policies, defaults | 10+ |
| Network | Device defaults, SNMP, VPN | 20+ |
| RADIUS | Servers, ports, sync | 15+ |
| Notifications | SMTP, SMS, channels | 15+ |
| Collections | Dunning, prepaid, enforcement | 15+ |
| Usage | Rating, FUP, thresholds | 10+ |
| Auth | JWT, sessions, MFA | 10+ |
| Provisioning | Workflows, NAS, defaults | 10+ |
| GIS | Sync, geocoding, map | 10+ |
| Scheduler | Celery, intervals | 5+ |
| Bandwidth | Streams, aggregation, Redis | 10+ |
| Comms | Meta, WhatsApp | 5+ |
| TR-069 | ACS defaults | 3+ |
| SNMP | Discovery intervals | 3+ |
| Audit | Logging config | 3+ |

Each card: domain name, description, settings count, last modified, "Configure" button

### 17b. Domain Settings Page (`/admin/settings/{domain}`)

Settings grouped by function within domain. Each setting shows:
- Label
- Current value (masked if secret)
- Input type (text, number, boolean toggle, dropdown, JSON editor)
- Default value indicator
- Source indicator (env var / database / default)
- Description/help text

#### Setting Types
- **Boolean**: Toggle switch
- **Integer**: Number input with min/max
- **String**: Text input (password input if secret)
- **JSON**: JSON editor with validation
- **Enum**: Dropdown with allowed values

#### Save behavior
- Save per-section (not per-setting)
- Validate before save
- Show "unsaved changes" indicator
- Cache invalidation on save

### 17c. Integration-Specific Settings Pages

**SMTP Settings** (`/admin/settings/notification` → SMTP section):
- Host, port, username, password (masked)
- From email, from name
- TLS/SSL toggles
- Test connection button → sends test email to admin

**SMS Settings** (`/admin/settings/notification` → SMS section):
- Provider selector (Twilio / Africa's Talking / Webhook)
- Dynamic fields based on provider:
  - Twilio: Account SID, Auth Token, From Number
  - Africa's Talking: API Key, Username
  - Webhook: URL, API Key
- Test send button → sends test SMS to admin phone

**Payment Gateway** (`/admin/settings/billing` → Payment section):
- Provider type (Stripe / PayPal / Custom)
- API keys (masked)
- Webhook URL (auto-generated, copyable)
- Test mode toggle

**RADIUS** (`/admin/settings/radius`):
- See Section 11 above (full RADIUS settings)

**Geocoding** (`/admin/settings/gis` → Geocoding section):
- Provider (Nominatim)
- Base URL, user agent, timeout
- Test geocode button (input address → show result on map)

### Service: `app/services/domain_settings.py`

```python
class DomainSettings(ListResponseMixin):
    @staticmethod
    def get_settings_dashboard(db: Session) -> dict:
        """All domains with setting counts and last modified."""

    @staticmethod
    def get_domain_settings(db: Session, domain: str) -> dict:
        """All settings for a domain with current values and metadata."""

    @staticmethod
    def update_domain_settings(db: Session, domain: str, updates: dict) -> dict:
        """Batch update settings for a domain."""
```

---

## 18. RESELLER MODULE (`/admin/resellers`)

**Accent color:** indigo (sub-module of subscribers)

### 18a. Resellers Dashboard

#### KPI Cards
- Total Resellers | Active | Total Subscribers (via resellers) | Revenue (via resellers)

#### Charts
**Left: Subscribers by Reseller** (horizontal bar, top 10)
**Right: Revenue by Reseller** (horizontal bar, top 10)

#### Table
Reseller Name | Subscribers | Revenue | Commission | Status | Actions

### 18b. Reseller Detail (`/admin/resellers/{id}`)

#### Header
- Reseller name + status badge
- Actions: Edit, Suspend, Impersonate (reseller portal)

#### Tabs

**Overview:** Contact info, organization, commission rate, created date

**Subscribers:** Table of subscribers under this reseller (with full filter/sort)

**Billing:** Revenue summary, commission due, payment history

**Activity:** Audit log for reseller actions

### Service: `app/services/subscriber.py` (Resellers class)

```python
class Resellers(ListResponseMixin):
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Reseller KPIs, subscriber/revenue distribution."""
```

---

## 19. LEGAL MODULE (`/admin/legal`)

**Accent color:** slate

### 19a. Documents List

#### Table
Title | Type (ToS, Privacy, AUP, SLA) | Version | Status (draft/published) | Last Updated | Actions

### 19b. Document Editor

- Title, type selector
- Rich text editor (or markdown editor with preview)
- Version control: save as new version
- Publish button (makes current version the active one)
- Preview as customer portal view

### 19c. Version History

#### Table
Version | Date | Author | Status | Changes Summary | Actions (view, restore, diff)

### Service: `app/services/legal.py`

```python
class LegalDocumentService:
    @staticmethod
    def get_dashboard_stats(db: Session) -> dict:
        """Document counts by type and status."""
```

---

## 20. CUSTOMER PORTAL (`/portal`)

**Design:** Consumer-grade, minimal, mobile-first.

### 20a. Dashboard (`/portal/dashboard`)

#### Welcome Section
- "Hello, {name}" + account status badge
- Current plan card: plan name, speed (download/upload), monthly cost

#### Cards Row (3 cards)

| Card | Content | Action |
|------|---------|--------|
| Balance | Current balance (colored: red if owing, green if credit) | Pay Now button |
| Next Bill | Amount + due date | View Invoice |
| Usage | Progress bar (used/allowance) or "Unlimited" | View Details |

#### Quick Actions
- Pay Now
- View Invoices
- Change Plan
- Get Support (if support system integrated)

#### Recent Activity (5 items)
- Payments made, invoices received, plan changes, service status changes

### 20b. Services (`/portal/services`)

#### Active Services
Cards (not table) for each subscription:
- Plan name + speed badge
- Status indicator
- Monthly cost
- Start date
- "Manage" button → service detail

#### Service Detail (`/portal/services/{id}`)
- Plan info (name, speed, price, data allowance)
- Connection info (IP, VLAN — if ISP shows this)
- Equipment info (router model, serial)
- Usage chart (data consumed, 30d)
- "Change Plan" button → plan comparison view
- "Report Issue" button

#### Change Plan (`/portal/services/{id}/change-plan`)
- Side-by-side plan comparison cards
- Current plan highlighted
- Upgrade/downgrade indicators (arrow up green, arrow down amber)
- Price difference shown
- Proration explanation
- Confirm button → creates service order

### 20c. Billing (`/portal/billing`)

#### Balance Card
- Current balance, credit/debit indicator
- "Make Payment" CTA

#### Recent Invoices (5 items)
Invoice # | Date | Amount | Status badge | Download PDF | Pay (if unpaid)

#### Link to Full Invoice History

#### Invoice Detail (`/portal/billing/invoices/{id}`)
- Invoice header (number, dates, status)
- Line items table
- Totals
- Download PDF button
- Pay button (if unpaid, links to payment flow)

#### Payment History
Date | Amount | Method | Reference | Status

#### Make Payment (`/portal/billing/pay`)
- Amount input (default: current balance)
- Payment method selector (saved methods + new)
- Confirmation step
- Receipt display

#### Payment Arrangements (`/portal/billing/arrangements`)
- View existing arrangements (installment schedule)
- Request new arrangement (if eligible)

### 20d. Usage (`/portal/usage`)

#### Current Period
- Usage bar: used / allowance (or "Unlimited")
- Days remaining in billing cycle
- Projected usage (extrapolated)
- Warning if approaching limit

#### Charts
- Daily usage (bar chart, current billing period)
- Monthly comparison (line chart, last 6 months)

#### Usage Breakdown (if available)
- By device, by time of day

### 20e. Profile (`/portal/profile`)

#### Sections
- **Personal Info**: Name, email, phone (editable)
- **Address**: Address fields (editable)
- **Password**: Change password form
- **Notification Preferences**: Email/SMS toggles for invoice, payment, service, marketing
- **Security**: MFA setup, active sessions
- **Documents**: Contracts, uploaded documents
- **Download Data**: GDPR data export request

### Service: `app/services/customer_portal.py`

```python
class CustomerPortalService:
    @staticmethod
    def get_dashboard_data(db: Session, subscriber_id: str) -> dict:
        """Everything for customer dashboard."""

    @staticmethod
    def get_plan_comparison(db: Session, current_offer_id: str) -> dict:
        """Available plans for upgrade/downgrade comparison."""

    @staticmethod
    def initiate_payment(db: Session, subscriber_id: str, amount: Decimal) -> dict:
        """Start payment flow, return gateway redirect or form."""
```

---

## 21. RESELLER PORTAL (`/reseller`)

**Design:** Focused admin subset, reseller-scoped.

### 21a. Dashboard (`/reseller/dashboard`)

#### KPI Cards
- My Subscribers (active/total) | Revenue This Month | Outstanding | New This Month

#### Charts
- Subscriber growth trend (line, 6 months)
- Revenue trend (area, 6 months)

#### Recent Subscribers Table (5 rows)

### 21b. Accounts (`/reseller/accounts`)

#### Search + Filter (status, plan, balance)

#### Table
Name | Account # | Plan | Status | Balance | Actions (view, create invoice)

#### Subscriber Detail (limited view)
- Contact info, plan info, billing summary
- No network details, no system info

### 21c. Billing (`/reseller/billing`) — NEW

#### Commission Summary
- Total revenue, commission rate, commission due, last payout

#### Invoices Table (reseller's subscribers)
- Filtered to reseller's subscribers only

### 21d. Reports (`/reseller/reports`) — NEW

#### Subscriber Growth (simple line chart)
#### Revenue Summary (simple bar chart)

### 21e. Fiber Map (`/reseller/network/fiber-map`)
- Coverage area map (read-only)
- Available capacity indicators

### Service: `app/services/reseller_portal.py`

```python
class ResellerPortalService:
    @staticmethod
    def get_dashboard_data(db: Session, reseller_id: str) -> dict:
        """Everything for reseller dashboard (scoped to reseller)."""

    @staticmethod
    def get_commission_summary(db: Session, reseller_id: str) -> dict:
        """Commission calculation for reseller."""
```

---

## Shared UI Components

### Component Library (`templates/components/`)

| Component | File | Usage |
|-----------|------|-------|
| Stat Card | `data/stats_card.html` | KPI cards on all dashboards |
| Data Table | `data/table_interactive.html` | All list pages |
| Pagination | `data/table_pagination.html` | All list pages |
| List interaction standard | Per-resource query/projection owner plus product-native components | Global Dotmac standard: Carbon table/filter/search/pagination behavior with WCAG 2.2 AA as the accessibility floor; no Carbon visual-theme requirement |
| Empty State | `data/empty_state.html` | All list pages when no data |
| Status Badge | `status_presentation_badge` in `ui/macros.html`; `StatusChip`/`StatusPill` in Flutter | Account, subscription, invoice, payment, outage-incident, device operational, customer connection-health, support-ticket, and field work-order displays consume the server semantic contract; concrete role colors come only from branding/theme tokens |
| Chart Container | `charts/*.html` | All dashboard charts |
| Toast Container | `feedback/toast_container.html` | All pages (notifications) |
| Confirm Modal | `modals/confirm_modal.html` | Delete/suspend actions |
| Loading Skeleton | `feedback/skeleton.html` | HTMX partial loads |
| Form Input | `forms/input.html` | All forms |
| Form Select | `forms/select.html` | All forms |
| CSRF Input | `forms/csrf_input.html` | All POST forms |
| Activity Panel | `data/recent_activity_panel.html` | Dashboards |
| Sidebar | `navigation/admin_sidebar.html` | Admin layout |

### Dashboard Template Pattern

Every module dashboard follows the same template structure:

```html
{% extends "layouts/admin.html" %}

{% block breadcrumbs %}...{% endblock %}

{% block content %}
<!-- KPI Cards Row -->
<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
    {% for card in stats.kpi_cards %}
    {% include "components/data/stats_card.html" %}
    {% endfor %}
</div>

<!-- Charts Row -->
<div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
    <div class="bg-white dark:bg-slate-800 rounded-xl shadow-sm border border-slate-200 dark:border-slate-700 p-6">
        {% include "components/charts/area_chart.html" %}
    </div>
    <div class="bg-white dark:bg-slate-800 rounded-xl shadow-sm border border-slate-200 dark:border-slate-700 p-6">
        {% include "components/charts/doughnut_chart.html" %}
    </div>
</div>

<!-- Attention / Tables Row -->
<div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
    <!-- Tables / Activity / Quick Actions -->
</div>
{% endblock %}
```

---

## Navigation Structure (Updated Sidebar)

```
DASHBOARD
  └─ Overview

CUSTOMERS
  ├─ Subscribers
  └─ Resellers

SERVICES
  ├─ Catalog Offers
  ├─ Subscriptions
  ├─ Add-ons
  ├─ Service Orders
  └─ Workflows

BILLING
  ├─ Dashboard
  ├─ Invoices
  ├─ Payments
  ├─ Credit Notes
  ├─ Accounts
  ├─ AR Aging
  ├─ Collections & Dunning
  ├─ Tax Rates
  └─ Payment Channels

NETWORK
  ├─ Dashboard
  ├─ Network Map
  ├─ Devices (OLTs, ONTs, CPEs, Core)
  ├─ Fiber Plant
  ├─ IP Management
  ├─ VLANs
  ├─ NAS Devices
  ├─ Monitoring
  └─ VPN / WireGuard

REPORTS
  ├─ Revenue
  ├─ Subscribers
  ├─ Churn
  ├─ Network Usage
  ├─ Technician
  └─ Collections

SETTINGS
  ├─ General (per-domain)
  ├─ RADIUS
  ├─ Integrations & Connectors
  ├─ Notifications
  ├─ Automation & Scheduler
  └─ Legal Documents

SYSTEM
  ├─ Dashboard
  ├─ Users
  ├─ Roles & Permissions
  ├─ API Keys
  ├─ Webhooks
  ├─ Audit Log
  ├─ Health
  └─ GIS / Mapping
```

---

## Centralized Service Architecture Summary

### Core Services (business logic, shared by API + web)

```
app/services/
├── dashboard.py              → DashboardStats
├── subscriber.py             → Organizations, Resellers, Subscribers, Accounts, Addresses
├── billing/
│   ├── invoices.py           → Invoices, InvoiceLines
│   ├── payments.py           → Payments, PaymentAllocations, PaymentChannels
│   ├── credit_notes.py       → CreditNotes
│   ├── ledger.py             → LedgerEntries
│   ├── tax.py                → TaxRates
│   ├── reporting.py          → BillingReporting (dashboard stats, AR aging, trends)
│   └── configuration.py      → BillingConfiguration
├── catalog/
│   ├── offers.py             → Offers, OfferVersions
│   ├── subscriptions.py      → Subscriptions
│   ├── add_ons.py            → AddOns
│   ├── profiles.py           → RegionZones, SlaProfiles, UsageAllowances
│   ├── policies.py           → PolicySets
│   └── radius.py             → RadiusProfiles (catalog-linked)
├── network/
│   ├── cpe.py                → CPEDevices, Vlans
│   ├── olt.py                → OLTDevices, PonPorts, OntUnits
│   ├── ip.py                 → IpPools, IPAssignments
│   └── fiber/                → Fiber infrastructure
├── provisioning.py           → ServiceOrders, Workflows, Appointments
├── radius.py                 → RadiusServers, RadiusClients, RadiusSyncJobs
├── notification.py           → Notifications, Templates, Deliveries, AlertPolicies
├── integration.py            → IntegrationJobs, IntegrationTargets
├── connector.py              → ConnectorConfigs
├── network_monitoring.py     → Alerts, AlertRules, DeviceMetrics
├── usage.py                  → UsageRecords, QuotaBuckets, UsageCharges
├── collections/              → Dunning, billing enforcement
├── wireguard.py              → WireGuardServers, Peers
├── gis.py                    → GeoLocations, GeoAreas, GeoLayers
├── reports.py                → ReportingService (all report types)
├── domain_settings.py        → DomainSettings
├── scheduler.py              → ScheduledTasks
├── legal.py                  → LegalDocumentService
├── audit.py                  → AuditEvents
├── rbac.py                   → Roles, Permissions
├── customer_portal.py        → CustomerPortalService
├── reseller_portal.py        → ResellerPortalService
├── billing_automation.py     → BillingAutomation
└── common.py                 → Shared utilities
```

### Web Services (template context builders only)

```
app/services/
├── web_admin_dashboard.py    → build_dashboard_context()
├── web_billing_*.py          → build_*_context(), handle_*()
├── web_catalog_*.py          → build_*_context(), handle_*()
├── web_network_*.py          → build_*_context(), handle_*()
├── web_subscriber_*.py       → build_*_context(), handle_*()
├── web_system_*.py           → build_*_context(), handle_*()
├── web_reports.py            → build_*_report_context()
├── web_integrations.py       → build_*_context()
├── web_usage.py              → build_*_context()
└── web_auth.py               → handle_login(), handle_logout()
```

### API Routes (thin wrappers → core services)

```
app/api/
├── billing.py                → billing_service.*
├── subscribers.py            → subscriber_service.*
├── catalog.py                → catalog_service.*  (NEW — if missing)
├── provisioning.py           → provisioning_service.*
├── network.py                → network_service.*  (NEW — if missing)
├── radius.py                 → radius_service.*   (NEW — if missing)
├── notifications.py          → notification_service.*
├── integrations.py           → integration_service.*
├── usage.py                  → usage_service.*    (NEW — if missing)
├── reports.py                → reporting_service.* (NEW — if missing)
├── settings.py               → domain_settings.*
├── webhooks.py               → webhook_service.*
├── audit.py                  → audit_service.*
├── scheduler.py              → scheduler_service.*
└── system.py                 → system_service.*   (NEW — if missing)
```

### Key Principle

Every `get_dashboard_stats()` method in a core service is callable from both:
- `GET /api/v1/{module}/dashboard` → returns JSON
- `GET /admin/{module}` → web service wraps into template context

This guarantees API consumers and admin UI users always see the same numbers.
