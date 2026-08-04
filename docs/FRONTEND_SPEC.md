# Frontend Developer Specification

> **Authority:** Implementation specification. Information selection, depth,
> state provenance, and actions are governed by
> `docs/UI_INFORMATION_AND_ACTION_STANDARD.md`; visual and interaction policy is
> governed by `docs/PRODUCTION_UI_BRIEF.md`.

> **Stack:** Jinja2 + HTMX + Alpine.js + Tailwind CSS v4
> **Layout:** Server-rendered templates consuming context dicts from Python web services
> **Hybrid frontend contracts** — server-rendered HTML/HTMX primarily consumes
> web-service context dictionaries; selected widgets and mobile clients consume
> versioned REST APIs. Each surface must use its declared contract owner.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [How It Works](#how-it-works)
3. [Admin Layout & Blocks](#admin-layout--blocks)
4. [UI Macro Library](#ui-macro-library)
5. [Design System](#design-system)
6. [Module Page Specs](#module-page-specs)
   - [Dashboard](#dashboard)
   - [Customers](#customers)
   - [Subscribers](#subscribers)
   - [Billing — Invoices](#billing--invoices)
   - [Billing — Payments](#billing--payments)
   - [Catalog — Offers](#catalog--offers)
   - [Catalog — Subscriptions](#catalog--subscriptions)
   - [Network — OLTs](#network--olts)
   - [Network — ONTs](#network--onts)
   - [Provisioning](#provisioning)
   - [Reports](#reports)
   - [VPN Management](#vpn-management)
   - [Resellers](#resellers)
   - [Notifications](#notifications)
   - [System / Admin](#system--admin)
7. [Model Field Reference](#model-field-reference)
8. [HTMX Interaction Map](#htmx-interaction-map)
9. [Form Patterns](#form-patterns)
10. [Template Conventions](#template-conventions)

---

## Architecture Overview

```
Browser Request
    │
    ▼
Web Route (app/web/admin/*.py)     ← thin wrapper, no logic
    │
    ▼
Web Service (app/services/web_*.py) ← builds context dict
    │
    ▼
Jinja2 Template (templates/admin/**/*.html)
    │
    ▼
HTML Response (with HTMX attributes for dynamic behavior)
```

**Key principle:** Routes are thin wrappers. ALL data assembly, filtering, aggregation, and business logic lives in web service files (`app/services/web_*.py`). The template receives a **context dict** — that dict is your "API response".

### List-query contract

Production list screens use `app.services.list_query.ListDefinition`,
`ListQuery`, and `PageMeta` rather than reconstructing query state in routes or
templates. A resource definition declares searchable, filterable, and sortable
capabilities plus page-size and sort defaults. Its projection service owns the
actual filter/search expressions, filtered count, stable ordering, and row
projection.

Canonical web parameters are `search`, declared resource filters, `sort`, `dir`,
`page`, and `per_page`. They remain in the URL for deep links, refresh, and
browser history. Search, filter, sort, or page-size changes reset to page one.
Every sort must include a unique tie-breaker, and filtering always precedes
pagination. Templates consume `ListQuery.url(...)` and `PageMeta`; they must not
hand-build parallel query strings or page calculations.

The customer admin list is the pilot owner in
`app.services.web_customer_lists`. New migrations should follow that boundary
and retire older pagination implementations incrementally. Under the global
Dotmac UI standard, interaction behavior follows Carbon's data-table/filter/
pagination patterns and WCAG 2.2 AA is the accessibility floor. This governs
behavior and accessibility, not the product's visual theme.

### Portal Account Health contract

Customer dashboard, customer service detail, reseller account detail, and the
customer mobile app consume `app.services.portal_account_health`. The first
viewport keeps account lifecycle, billing mode, currency-separated receivables,
prepaid funding, service access, subscription-scoped live-session evidence,
connection/outage diagnosis, freshness, and the canonical next action together.
Jinja surfaces share `templates/components/portal/account_health.html`; mobile
uses `GET /api/v1/me/account-health`. Neither client derives those semantics or
issues a parallel connection-status request. See
`docs/designs/PORTAL_ACCOUNT_SERVICE_HEALTH.md` for ownership, query budget, and
the retired contracts.

The legacy `/api/v1/tables/customers/data` endpoint is a compatibility adapter,
not another list owner. It maps its offset parameters and supported aliases into
the customer `ListQuery`, delegates filtering/counting/stable ordering/paging to
`app.services.web_customer_lists`, and applies saved configurable columns only
after that selection. Unsupported legacy scalar filters or sort fields return
HTTP 400 rather than silently creating parallel customer semantics.

The `/api/v1/tables/subscribers/data` endpoint follows the same compatibility
rule through `app.services.web_subscriber_lists`. Subscriber scope and full-text
search reuse `app.services.subscriber.Subscribers.query`; the compatibility
projection owns only declared filters, stable ordering, paging, and configured
column serialization. It is not backed by a separate live admin screen: the
current subscriber-named web facade resolves to `/admin/customers`. Table reads
must never generate subscriber numbers or commit any other domain mutation.

### Bulk-action contract

Production tables do not infer bulk scope or action availability in templates.
`app.services.bulk_actions` declares selection behavior plus action label,
description, semantic tone, preview/confirmation requirement, execution mode,
and result-reference vocabulary. A resource web projection omits unauthorized
actions; command routes still enforce permission and command services re-check
scope and business eligibility.

The customer list is the first pilot. Its header checkbox means the visible
page only.
After a full-page selection, an explicit second control may select all customers
matching the canonical `ListQuery` search and filters. No selection means no
bulk controls, and an empty ID array is invalid rather than an alias for all
filtered rows. Search, filter, and page-size changes clear selection.

Customer bulk update and message commands are preview-first. Confirmation sends
the previewed matched count and exact-membership token; execution re-resolves
the canonical cohort and returns HTTP 409 if it changed, including a same-count
membership replacement. Responses report real matched/processed/skipped
outcomes, while queued messaging exposes notification IDs as the durable result
references. Selection counts use an `aria-live` status, checkboxes have record
labels and an indeterminate page state, and filtered selection remains visually
and textually distinct from selected IDs.

The invoice list is the second adoption. Its existing
`app.services.web_billing_overview` owner declares search, filters, stable sorts,
page clamping, status summaries, and the uncapped CSV export scope through
`ListQuery` and `PageMeta`. The full page and HTMX request share the same list
and table partials. Invoice selection is page-only; permission-filtered actions
carry server-owned row eligibility. Issue, send, void, mark-paid, and PDF
generation preview exact eligible/skipped impact before confirmation. The
confirmation token fingerprints both membership and eligibility, so execution
returns HTTP 409 if status or scope drift changes the previewed impact. The
invoice command service re-checks eligibility and audits only processed IDs.
Invoice and payment list periods use explicit optional `start_date` and
`end_date` filters against each document's UTC `created_at`. Both calendar dates
are inclusive; owners translate the end date to the exclusive start of the next
UTC day. List totals, pagination, status summaries, deep links, and CSV exports
consume the same normalized range.

The support-ticket queue is the next list adoption. `app.services.support.Tickets`
owns the canonical filtered domain query, while
`app.services.web_support_tickets` declares the admin list capabilities and owns
query normalization, exact counts, stable sorting, clamped pages, status-summary
links, and complete CSV scope. The route is a thin adapter, and full-page and
HTMX reads share `_list.html` and `_table.html`. Advanced filters are validated
and serialized by the same owner used by export; templates consume `ListQuery`
and `PageMeta` rather than assembling query strings or estimating totals.

Support-ticket bulk update is page-selection only. The authorized action
projection comes from `app.services.web_support_ticket_bulk_actions`; templates
do not infer update permission or row eligibility. The modal submits one shared
status/priority/primary-assignee change set to
`app.services.web_support_ticket_bulk`, which previews exact membership,
proposal-bound eligibility, and skipped reasons without side effects. Confirmed
execution sends the previewed matched count and scope token, rejects membership,
eligibility, or proposed-change drift with HTTP 409, and reports processed and
skipped outcomes. The command delegates eligible rows through
`app.services.support.Tickets.update`, preserving lifecycle consequences.

### Semantic status contract

Lifecycle services own raw account, subscription, invoice, payment, outage-incident,
support-ticket, and field work-order status values and transitions;
`app.services.device_operational_status` owns derived NOC device state;
`app.services.topology.connection_status` owns the separate customer-safe
connection-health verdict and diagnostic wording;
`app.services.status_presentation` owns their UI
meaning. Web contexts and API schemas carry a `StatusPresentation` with
`value`, `label`, semantic `tone`, and an icon key. Templates consume
`status_presentation_badge(...)`; customer mobile consumes
`StatusChip.fromPresentation(...)` for subscriptions, invoices, and tickets;
field mobile consumes `StatusPill` from job, manager, and map projections.
Admin outage consoles consume the same badge macro, and CRM outage rows carry
`status_presentation` alongside the raw `status`/`state` compatibility fields.
Payment list/detail/customer/reseller projections use the same contract without
reinterpreting settlement or refund state. NOC inventory, core-device detail,
monitoring, worklist, and map projections
consume the same contract. `NetworkDeviceRead` carries the binary
`operational_status`, its `operational_reason`, and `status_presentation`.
Freshness and retry-pending evidence stay inside the verification lifecycle and
are not client-visible device states.
Customer connection API/portal/reseller projections carry
`status_presentation` beside the raw verdict; customer web and mobile map only
its tone/icon to platform-native visuals. Raw RADIUS session indicators remain
separate observations and do not masquerade as the diagnostic verdict.

The shared tones are `positive`, `info`, `warning`, `negative`, and `neutral`.
They are not colors. Branding owns the concrete seed for each role and generates
the light/dark web scales; customer and field mobile read the same
`BRAND_SEMANTIC_*_COLOR` inputs. A renderer may select only a role token and
non-color icon. Unknown or old-backend values fall back to neutral. A client
must not add its own state-to-tone or tone-to-literal-color switch. Raw
work-order values remain available
for transition eligibility, filters, and offline queue behavior; presentation
fields are read-only UI metadata. Support status settings may expose a subset of
the ticket lifecycle vocabulary, but they do not configure semantic tones or
platform colors.

Device operational presentation does not re-derive state. `working` is
positive and `not_working` is negative. Administrative lifecycle, impairment,
and verification outcomes are separate reason/evidence fields; they never add
another device badge. A `not_working` verification-failure reason is
non-alarming and must say that operation could not be confirmed rather than
claiming a physical failure.

---

## How It Works

### GET (List/Detail Page)
```python
# Route (you don't write this — it exists)
@router.get("/billing", response_class=HTMLResponse)
def billing_index(request: Request, db: Session = Depends(get_db)):
    context = web_billing_overview.build_invoices_list_data(db, page=1, per_page=25)
    context["request"] = request
    context["active_page"] = "billing"
    return templates.TemplateResponse("admin/billing/index.html", context)
```

### POST (Form Submission)
```python
# Always redirects after POST (POST-Redirect-GET pattern)
@router.post("/billing/invoices/create", response_class=HTMLResponse)
def create_invoice(request: Request, db: Session = Depends(get_db)):
    web_billing_invoices.handle_create(request, db)
    return RedirectResponse(url="/admin/billing", status_code=303)
```

### HTMX Partial
```python
# Returns a fragment, not a full page
@router.get("/dashboard/stats", response_class=HTMLResponse)
def dashboard_stats(request: Request, db: Session = Depends(get_db)):
    context = web_admin_dashboard.dashboard_stats_partial(request, db)
    return templates.TemplateResponse("admin/dashboard/_stats.html", context)
```

---

## Admin Layout & Blocks

**File:** `templates/layouts/admin.html` (extends `base.html`)

### Available Blocks

| Block | Location | Purpose |
|-------|----------|---------|
| `{% block breadcrumbs %}` | Top nav bar, left | Navigation path |
| `{% block page_header %}` | Above content | Title, subtitle, icon, action buttons |
| `{% block content %}` | Main area | Page-specific content |
| `{% block content_container_class %}` | Wrapper | Override container classes |

### Built-in Features (you get these for free)
- **Sidebar** — 240px fixed, collapsible, with nav menu and user profile
- **Dark mode toggle** — Alpine.js store, class-based
- **Global search** — Cmd+K keyboard shortcut
- **Notification dropdown** — Unread count badge, loads via HTMX
- **Mobile responsive** — Hamburger menu, sidebar collapses

### Required Context (auto-injected by middleware)
```python
{
    "request": Request,              # Required by Jinja2
    "current_user": {                # Admin user
        "name": str,
        "email": str,
        "initials": str,
    },
    "sidebar_stats": {               # Sidebar branding
        "app_name": str,
        "sidebar_logo_url": str,
        "sidebar_logo_dark_url": str,
        "notifications_unread": int,
    },
}
```

### Template Skeleton
```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import page_header, stats_card, action_button, ... %}

{% block breadcrumbs %}
<a href="/admin/dashboard" class="text-slate-500 hover:text-slate-700">Dashboard</a>
<span class="mx-2 text-slate-400">/</span>
<span class="text-slate-900 dark:text-white">Current Page</span>
{% endblock %}

{% block page_header %}
{{ page_header(title="Page Title", subtitle="Description", icon=icon_users(), color="amber", color2="orange") }}
{% endblock %}

{% block content %}
<!-- Your page content here -->
{% endblock %}
```

---

## UI Macro Library

**File:** `templates/components/ui/macros.html` (~1300 lines, 40+ macros)

Import what you need:
```html
{% from "components/ui/macros.html" import page_header, stats_card, status_badge, data_table, table_head, table_row, row_actions, row_action, empty_state, card, search_input, filter_select, filter_bar, pagination, action_button, tabs, info_row, detail_header, icon_badge, info_banner, type_badge, step_indicator, progress_bar, timeline_item, metric_row, info_grid, section_divider, setting_row, setting_toggle, submit_button, danger_button, warning_button, validated_input, avatar, vendor_badge, connection_status, alert_count_badge, status_filter_card, ambient_background %}
```

### Layout & Page Structure

| Macro | Parameters | Purpose |
|-------|-----------|---------|
| `ambient_background` | `color1, color2, position` | Legacy compatibility macro; do not use on new operational pages |
| `page_header` | `title, subtitle, icon, color, color2, actions, breadcrumbs` | Compact page context with one primary action |
| `detail_header` | `title, subtitle, icon, color, color2, back_href, status` | Detail page header with back button |
| `card` | `title, icon, color, color2, actions, padding` | Generic card container |
| `filter_bar` | (caller block) | Container for search/filter controls |
| `tabs` | `items, active, color` | Tab navigation |
| `section_divider` | `label` | Horizontal divider with optional label |
| `info_grid` | (caller block) | Two-column key-value grid |

### Data Display

| Macro | Parameters | Purpose |
|-------|-----------|---------|
| `stats_card` | `label, value, icon, color, color2, href, change, change_type, tone` | KPI metric card; status KPIs use a branding-owned semantic `tone` |
| `status_badge` | `status, variant, pulse, size, show_icon, icon` | Low-level accessible status pill renderer |
| `status_presentation_badge` | `presentation, pulse, size, show_icon` | Domain status pill from the server-owned semantic contract |
| `type_badge` | `type, size` | Person/Organization badge |
| `info_row` | `label, value, icon` | Key-value detail row |
| `metric_row` | `label, value, color, mono` | Label-value with colored dot |
| `progress_bar` | `value, max, label, show_text, size, color` | Horizontal bar with auto-coloring |
| `timeline_item` | `title, description, time, color, tone, is_last` | Vertical timeline entry; prefer the branding-owned semantic `tone` for status activity |
| `connection_status` | `status, label, last_seen` | Online/offline dot indicator |
| `alert_count_badge` | `count, color` | Count pill, hidden when 0 |
| `icon_badge` | `icon, color, color2, size` | Compact orientation icon; semantic state remains separate |
| `avatar` | `name, type, size, color` | User avatar with initials |
| `vendor_badge` | `vendor, size` | Vendor/provider badge |

### Tables

| Macro | Parameters | Purpose |
|-------|-----------|---------|
| `data_table` | `title, icon, color, count, actions` | Table wrapper with header |
| `table_head` | `columns` | Styled `<thead>` |
| `table_row` | `color` | Stable `<tr>` with restrained hover state |
| `row_actions` | (caller block) | Action button container in last column |
| `row_action` | `href, icon, title, color, hx_attrs` | Individual row action button |
| `empty_state` | `title, message, icon, color, color2, action_label, action_href, colspan` | Empty table state with CTA |
| `pagination` | `page, total_pages, total, per_page, base_url, hx_target, color, extra_params` | HTMX-powered pagination |
| `status_filter_card` | `label, value, icon, href, color, color2, is_active` | Clickable filter card |

### Buttons & Forms

| Macro | Parameters | Purpose |
|-------|-----------|---------|
| `action_button` | `label, href, icon, variant, color, color2, size, hx_attrs` | Primary/secondary/ghost/danger button |
| `submit_button` | `label, loading_label, icon, color, size` | Form submit with loading state |
| `danger_button` | `label, confirm_title, confirm_message, action_url, method, size, icon` | Delete with confirmation modal |
| `warning_button` | `label, confirm_title, confirm_message, action_url, method, size, icon` | Warning action with confirmation |
| `action_form` | `form` (`ActionForm`) | Server-owned action fields, impact, confirmation, disabled reason, submitted values, and structured errors |
| `search_input` | `name, value, placeholder, color, hx_attrs` | Search input with icon |
| `filter_select` | `name, options, value, label, color, attrs` | Styled select dropdown |
| `validated_input` | `name, label, type, value, placeholder, required, validation_url, help_text, icon` | Input with real-time validation |
| `setting_row` | `label, description, source` | Settings page row |
| `setting_toggle` | `name, value` | Toggle switch |
| `step_indicator` | `steps, current, color` | Multi-step wizard indicator |
| `info_banner` | `message, icon, color, actions` | Contextual info banner |

### Icon Macros

All accept optional `class` parameter for sizing.

**Small icons** (default `h-4 w-4`): `icon_plus`, `icon_view`, `icon_edit`, `icon_delete`, `icon_calendar`, `icon_clock`, `icon_check`, `icon_x`, `icon_link`, `icon_mail`, `icon_refresh`, `icon_filter`, `icon_search`, `icon_download`, `icon_upload`

**Large icons** (default `h-6 w-6 text-white`): `icon_users`, `icon_user`, `icon_server`, `icon_network`, `icon_location`, `icon_ticket`, `icon_invoice`, `icon_check_circle`, `icon_lightning`, `icon_bell`, `icon_clipboard`, `icon_archive`, `icon_credit_card`, `icon_trending_up`, `icon_chart_bar`, `icon_shield`, `icon_cog`, `icon_puzzle`, `icon_heart`, `icon_cpu`, `icon_warning`, `icon_building`, `icon_database`

---

## Design System

### Legacy Module Accent References

The named palettes below document existing surfaces; they are not color
authority and must not be copied into new or touched code. Branding owns
identity colors through the generated `primary-*` and `accent-*` tokens.
Semantic state uses the generated `status-*` role tokens. Any categorical
palette must be centralized in the theme layer instead of being embedded in a
service, template, or chart payload.

`/branding/theme.css` is the web color compatibility boundary. It remaps every
non-neutral legacy Tailwind palette (`red`, `amber`, `emerald`, `blue`,
`violet`, and peers) onto the configured primary, accent, or semantic scale.
Those old class names therefore remain safe while a template is migrated, but
they are not authoring vocabulary for new code. Charts and maps use the ordered
`--color-data-1` through `--color-data-7` tokens, resolved for JavaScript by
`themeColor(...)`. Structural surfaces, text, borders, shadows, white, and black
remain design-system foundation tokens rather than tenant brand identity.

| Module | color | color2 | Used In |
|--------|-------|--------|---------|
| Subscribers/Customers | `amber` | `orange` | page_header, action_button, filter_bar |
| Billing/Invoices | `emerald` | `teal` | Invoice detail, payment status, KPIs |
| Catalog/Offers | `violet` | `purple` | Plan cards, pricing, subscription stats |
| Network/Infrastructure | `blue` | `indigo` | Device status, topology, IP management |
| Speed Tests | `cyan` | `sky` | Speedtest results, bandwidth |
| Provisioning | `amber` | `orange` | Service orders, workflow steps |
| System/Admin | `slate` | `gray` | Settings, users, roles |
| VPN/WireGuard | `cyan` | `teal` | VPN status, peers |
| Reports/Analytics | `teal` | `emerald` | Charts, KPIs |
| GIS/Mapping | `green` | `emerald` | Map markers, coverage |
| Notifications | `rose` | `pink` | Alerts, on-call |
| Integrations | `indigo` | `blue` | Connectors, webhooks |

### Legacy Status Badge References

These names describe the intended meaning of older variants, not their literal
color implementation. Migrate a touched domain to `StatusPresentation`; the
shared renderer resolves its semantic tone through branding.

| variant | When to use | Visual |
|---------|-------------|--------|
| `"active"` | Active subscriber/subscription | Indigo/blue gradient |
| `"online"` | Device connected | Emerald with pulse |
| `"offline"` | Device disconnected | Red/rose gradient |
| `"pending"` | Awaiting action | Amber semi-transparent |
| `"paid"` | Invoice paid | Emerald semi-transparent |
| `"overdue"` | Past-due invoice | Red semi-transparent |
| `"draft"` | Not submitted | Slate |
| `"warning"` | Needs attention | Amber/orange gradient |
| `"error"` | Failed operation | Red semi-transparent |
| `"info"` | Informational | Blue semi-transparent |
| `"maintenance"` | Under maintenance | Amber |
| `"decommissioned"` | Retired device | Slate |

### Typography

| Element | Classes |
|---------|---------|
| Display font | `font-display` (Outfit, `letter-spacing: -0.02em`) |
| Body font | `font-body` / `font-sans` (Plus Jakarta Sans) |
| Page heading | `font-display font-bold tracking-tight` |
| Body text | `text-sm` (dense data) or `text-base` (readable) |
| Financial values | `tabular-nums font-mono` |
| IP/MAC addresses | `font-mono` |
| Technical identifiers | `font-mono text-[13px]` for ONT serials, PPPoE usernames, VLAN IDs, account numbers, port references |
| Labels | `text-sm font-medium text-slate-700 dark:text-slate-300` |
| Helper text | `text-xs text-slate-500 dark:text-slate-400` |

### Dark Mode

Class-based toggle. Always pair light/dark:
```html
bg-white dark:bg-slate-800
text-slate-900 dark:text-white
border-slate-200 dark:border-slate-700
bg-slate-50 dark:bg-slate-900      <!-- alternating rows -->
```

---

## ISP UX Conventions

These conventions make the design system fit ISP operations instead of a generic back-office app.

### Identity Model

- Subscriber identity is dual-track:
  - human identity: name, phone, address, zone
  - service identity: account number, subscriber code, PPPoE username, ONT serial, OLT/PON location, IP, MAC
- Detail pages should show both tracks in the summary region when relevant.
- Technical identifiers should be easy to copy and visually distinct from narrative metadata.

### Search Model

- Subscriber and network search inputs should support:
  - name
  - phone
  - account number
  - invoice number
  - PPPoE username
  - ONT serial
  - IP / MAC
  - OLT or port reference
- Placeholder copy should teach supported identifiers instead of using generic text like `Search...`.

### State Semantics

- `online` is not the same as `healthy`.
- `offline` is not the same as `degraded`.
- Subscriber service state, payment state, and device state should not be collapsed into one badge.
- When a page mixes billing and network information, keep those states visually separate and explicitly labeled.

### NOC / Outage Presentation

- Favor grouped impact surfaces:
  - affected zone / site
  - OLT or PON blast radius
  - affected subscriber count
  - probable cause / primary reason
- Alarm feeds are supporting detail, not the only way to understand impact.
- Current impact belongs above historical trend.

### Provisioning / Activation Presentation

- Provisioning screens should show prerequisites before execution.
- Preview / dry-run output is part of the work surface, not an advanced hidden panel.
- Async execution needs stable progress states: pending, running, failed, retryable, completed.

### Map / Topology Presentation

- Maps must pair with a synchronized list, table, or detail rail.
- Marker color cannot be the only severity cue.
- Clusters should expose count and severity mix before drill-in.

## Module Page Specs

Each spec documents: URL, template path, context dict shape, and HTMX partials.

---

### Dashboard

#### `GET /admin/dashboard`
**Template:** `admin/dashboard/index.html`

```python
{
    # ── KPI Stats (compact decision layer; 4-6 items shown) ──
    "stats": {
        "total_subscribers": int,
        "active_subscribers": int,
        "subscribers_change": int,          # vs previous period
        "monthly_revenue": float,
        "mrr": float,                       # Monthly Recurring Revenue
        "arpu": float,                      # Average Revenue Per User
        "revenue_change": float,
        "system_uptime": float,             # percentage
        "ar_current": float,               # Accounts Receivable buckets
        "ar_30": float,
        "ar_60": float,
        "ar_90": float,
        "suspended_accounts": int,
        "orders_new": int,
        "orders_qualification": int,
        "orders_scheduled": int,
        "orders_in_progress": int,
        "orders_pending_activation": int,
        "orders_completed_today": int,
        "olts_working": int,
        "olts_total": int,
        "onts_working": int,
        "onts_total": int,
        "alarms_critical": int,
        "alarms_major": int,
        "alarms_minor": int,
        "alarms_warning": int,
        "bandwidth_current": float,
        "bandwidth_peak": float,
        "bandwidth_capacity": float,
        "jobs_completed": int,
        "jobs_total": int,
        "techs_active": int,
        "churn_rate": float,
    },

    # ── Exception / Attention Layer ──
    "attention_items": list[dict],          # [{label, href, severity}]
    "pending_orders": int,
    "total_alarms": int,

    # ── Supporting Operational Summaries ──
    "online_count": int,                    # active RADIUS sessions
    "monitoring_summary": {
        "devices_working": int,
        "devices_not_working": int,
        "devices_total": int,
    },
    "onu_summary": {
        "online": int,
        "offline": int,
        "low_signal": int,
        "total": int,
    },

    # ── Work Surface Data ──
    "recent_activity": list,                # raw audit events if needed elsewhere
    "recent_activities": list[dict],        # [{type, message, detail, time}]
    "recent_subscribers": list,             # newest signups

    # ── Lower Live Partial(s) ──
    "server_health": dict,                  # CPU, memory, disk metrics
    "server_health_status": dict,           # overall status + issues

    # ── Permission Gates ──
    "show_financials": bool,
    "show_network": bool,
    "show_subscribers": bool,

    # ── Standard ──
    "now": datetime,
    "active_page": "dashboard",
    "active_menu": "dashboard",
}
```

Dashboard implementation notes:
- The first viewport should expose one true work surface such as priority activity, pending installs, outage impact, or collections risk.
- Launchpads are support navigation, not the primary payload.
- If network and billing metrics both appear, keep them grouped by task instead of mixing them into one undifferentiated KPI wall.

#### Dashboard Composition Rules

- Use one primary action in the page header.
- Keep the first decision layer to 4-6 KPI tiles.
- Show only exception-based items in the attention banner.
- Group quick links by workflow (`Operations`, `Network`, `Control`), not by every available module.
- The first work surface should be operational content such as activity, queue state, or recent records.
- Lower HTMX partials like server health belong below or beside the main work surface, not above it.

#### HTMX Partials

| Endpoint | Template | Trigger |
|----------|----------|---------|
| `GET /admin/dashboard/stats` | `_stats.html` | Auto-refresh polling for KPI strip |
| `GET /admin/dashboard/activity` | `_activity.html` | Auto-refresh polling for activity work surface |
| `GET /admin/dashboard/server-health` | `_server_health.html` | Auto-refresh polling for lower live status widget |

---

### Customers

#### `GET /admin/customers`
**Template:** `admin/customers/index.html`

```python
{
    "customers": list[dict],            # Each:
    #   {
    #       "id": str (UUID),
    #       "type": "person" | "organization",
    #       "name": str,
    #       "email": str | None,
    #       "phone": str | None,
    #       "is_active": bool,
    #       "created_at": datetime,
    #       "raw": Subscriber | Organization,  # full ORM object
    #   }
    "stats": {
        "total_customers": int,
        "total_people": int,
        "total_organizations": int,
    },
    "page": int,
    "per_page": int,
    "total": int,
    "total_pages": int,
    "search": str | None,
    "customer_type": str | None,        # filter: "person" | "organization" | None
    "active_page": "customers",
}
```

#### `GET /admin/customers/{type}/{id}` (Person Detail)
**Template:** `admin/customers/detail.html`

```python
{
    "customer": Subscriber,             # full ORM object
    "customer_type": "person",
    "customer_name": str,

    # ── Related Records ──
    "subscribers": list[Subscriber],
    "accounts": list[Subscriber],
    "subscriptions": list[Subscription],
    "restorable_subscription_ids": set[str], # lifecycle-owner restore candidates
    "account_lookup": dict[str, Subscriber],  # account_id → Subscriber
    "invoices": list[Invoice],
    "payments": list[Payment],
    "notifications": list[Notification],

    # ── Address & Location ──
    "addresses": list[Address],
    "primary_address": Address | None,
    "map_data": {                       # or None
        "center": [lat, lon],
        "geojson": {"type": "FeatureCollection", "features": [...]},
    },
    "geocode_target": dict | None,

    "contacts": list[dict],             # empty for persons

    # ── Statistics ──
    "stats": {
        "total_subscribers": int,
        "total_subscriptions": int,
        "active_subscriptions": int,
        "balance_due": float,
        "total_addresses": int,
        "total_contacts": int,
    },
    "financials": {
        "total_invoiced": float,
        "total_paid": float,
        "overdue_invoices": int,
        "last_payment": Payment | None,
        "last_invoice": Invoice | None,
        "monthly_recurring": float,
    },
    "account_health": PortalAccountHealth,  # active services only in Customer 360;
                                            # history remains in the Service tab

    "has_active_subscribers": bool,
    "has_any_subscribers": bool,
    "activity_items": list[dict],       # [{type, title, description, timestamp}]
    "customer_user_access": dict,       # portal login state
    "can_activate_subscriptions": bool, # RBAC-owned visibility for restore actions
    "can_suspend_subscriptions": bool,  # RBAC-owned visibility for suspend actions
    "active_page": "customers",
}
```

**Account tab presentation contract:** This detail surface supports customer-care
and provisioning staff verifying customer identity, contactability, service
location, account linkage, and access readiness. Its authoritative read owner is
`app.services.web_customer_details`; action eligibility and execution remain with
their existing customer, access, identity, and CRM owners.

- The first decision layer remains the four-item Active Services, Balance Due,
  Service Orders, and Monthly Revenue summary.
- `Customer Profile & Location` groups contact, identity, and applicable business
  details beside the selected service-location map. The information column comes
  first in source and mobile order. Desktop uses a large map; missing coordinates
  retain the same location footprint and show `Location not set` with the existing
  geocoding or add-address action.
- `Account & Access` groups Subscriber Accounts (including the existing Convert
  action), Portal Access, and CRM Sync without changing their status meanings,
  permissions, confirmations, or command destinations.
- Subscription records are investigated and managed in the Services tab; the
  Account tab does not duplicate an Active Subscriptions preview.

The **All Subscriptions** work surface exposes one visible `Restore` row action
for blocked, suspended, stopped, or disabled services only when the authenticated
operator has `catalog:write` or `subscription:activate`. The action consumes the
canonical subscription-lifecycle preview, shows the exact billing, access, and
session impact, captures an operational reason, and confirms against the
reviewed state head before execution. The lifecycle endpoint rechecks the
kind-specific permission and current eligibility; the template is visibility,
not authorization.

#### `GET /admin/customers/{type}/{id}` (Organization Detail)
Same shape as person, plus:
```python
{
    "customer": Organization,
    "customer_type": "organization",
    "contacts": list[dict],             # [{id, first_name, last_name, role, title, is_primary, email, phone}]
}
```

#### Form Actions

| Method | URL | Action | Redirect |
|--------|-----|--------|----------|
| POST | `/admin/customers/new` | Create person or org | `/admin/customers/{type}/{id}` |
| POST | `/admin/customers/{type}/{id}/edit` | Update | Back to detail |
| DELETE | `/admin/customers/{type}/{id}` | Deactivate/delete | `/admin/customers` |
| POST | `/admin/customers/addresses` | Add address | Back to detail |
| POST | `/admin/customers/contacts` | Add contact | Back to detail |
| POST | `/admin/customers/bulk/status` | Bulk status change | `/admin/customers` |
| POST | `/admin/customers/bulk/delete` | Bulk delete | `/admin/customers` |

---

### Subscribers

#### `GET /admin/subscribers/{id}`
**Template:** `admin/subscribers/detail.html`

This is the **richest detail page** in the system:

```python
{
    "subscriber": Subscriber,

    # ── Service Records ──
    "accounts": list[Account],
    "subscriptions": list[Subscription],        # active, max 10
    "all_subscriptions": list[Subscription],    # all statuses
    "service_online_status": dict[str, bool],           # subscription_id → is_online
    "invoices": list[Invoice],
    "payments": list[Payment],
    "dunning_cases": list[DunningCase],
    "service_orders": list[ServiceOrder],
    "notifications": list[Notification],

    # ── Financial Stats ──
    "stats": {
        "monthly_bill": float,
        "balance_due": float,
        "credit_issued": float,
        "current_balance": float,
        "has_credit_adjustment": bool,
        "data_usage": str,
    },

    # ── Address & Location ──
    "addresses": list[Address],
    "primary_address": Address | None,
    "map_data": dict | None,
    "geocode_target": dict | None,

    # ── Contacts & Org ──
    "contacts": list[dict],                     # [{id, type, label, value, is_primary}]
    "organization_members": list[dict],         # [{id, name, email, is_active, is_current}]

    # ── Speed Tests ──
    "speedtests": list[SpeedTestResult],
    "speedtest_performance_rows": list[dict],   # [{test, down_ratio_pct, up_ratio_pct, is_underperforming}]
    "speedtest_chart": {
        "labels": list[str],
        "download": list[float],
        "upload": list[float],
    },
    "speedtest_plan": {
        "download_mbps": float,
        "upload_mbps": float,
    },
    "speedtest_underperforming_count": int,

    # ── Equipment ──
    "equipment": list[dict],                    # [{type, model, serial, online, detail_url, tr069_url}]
    "primary_ont_url": str | None,
    "primary_ont_tr069_url": str | None,

    # ── Enrichment ──
    "reseller_name": str | None,
    "last_online": str | None,
    "billing_email": str | None,
    "gps_coordinates": str | None,
    "access_credentials": list[AccessCredential],
    "nas_device_names": dict[str, str],         # subscription_id → NAS name
    "comms_email_count": int,
    "comms_sms_count": int,

    # ── Billing Config ──
    "billing_config": {
        "category": str | None,
        "billing_day": int | None,
        "payment_due_days": int | None,
        "grace_period_days": int | None,
        "min_balance": float | None,
        "billing_enabled": bool,
        "auto_create_invoices": bool,
        "send_billing_notifications": bool,
    },

    # ── Timeline ──
    "timeline": list[dict],                     # [{id, type, title, detail, is_comment, is_todo, is_completed, attachments, time}]

    # ── Form Helpers ──
    "offers": list[CatalogOffer],
    "subscription_statuses": list[str],
    "contract_terms": list[str],
    "subscriber_user_access": dict,

    "active_page": "subscribers",
}
```

`dunning_cases` and subscriber/subscription status are read projections. The
frontend must not infer that a case, payment, `blocked`, or `suspended` status
permits suspension or restoration. Dunning actions and financial restoration
consume the owner-produced `FinancialAccessConsequence` preview/result; RADIUS
delivery state is a separate network projection. Financial cards consume the
customer financial-position read model and do not recompute receivables,
prepaid funding, or restoration amounts from the listed invoices/payments.
Subscriber billing context does not calculate a future block date from balance,
grace, or metadata; only a confirmed owner consequence may describe an access
transition.

---

### Billing — Invoices

The filtered invoice CSV at `GET /admin/billing/invoices/export.csv` uses the
same uncapped filter and stable-sort scope as the list. Its customer identity
column is `customer_name`, populated from the same customer display-name
contract used by the invoice table; internal account UUIDs are not exported.

#### `GET /admin/billing`
**Template:** `admin/billing/index.html`

```python
{
    "invoices": list[Invoice],                  # ORM objects

    "status_totals": {                          # per-status aggregation
        "draft":          {"count": int, "amount": float},
        "issued":         {"count": int, "amount": float},
        "partially_paid": {"count": int, "amount": float},
        "paid":           {"count": int, "amount": float},
        "overdue":        {"count": int, "amount": float},
        "void":           {"count": int, "amount": float},
        "all":            {"count": int, "amount": float, "due_total": float, "received_total": float},
    },

    # ── Pagination ──
    "page": int,
    "per_page": int,
    "total": int,
    "total_pages": int,

    # ── Filters ──
    "account_id": str | None,
    "selected_partner_id": str | None,
    "partner_options": list[dict],              # [{id, name}]
    "status": str | None,
    "proforma_only": bool,
    "proforma_summary": {"count": int},
    "customer_ref": str | None,
    "search": str | None,
    "start_date": str | None,                  # inclusive YYYY-MM-DD, UTC
    "end_date": str | None,                    # inclusive YYYY-MM-DD, UTC

    "active_page": "billing",
}
```

#### `GET /admin/billing/invoices/{id}`
**Template:** `admin/billing/invoices/detail.html`

```python
{
    "invoice": Invoice,                         # ORM object with .lines, .account, etc.
    "tax_rates": list[TaxRate],
    "credit_notes": list[CreditNote],
    "activities": list[dict],                   # [{title, description, occurred_at}]
    "pdf_export": PdfExport | None,
    "is_proforma": bool,
    "prepaid_draft_reconciliation_preview": PrepaidDraftReconciliationPreview | None,
    "active_page": "billing",
}
```

An actionable prepaid draft exposes **Reconcile prepaid draft** only to staff
with `billing:invoice:update`. Its POST preview renders the authoritative
payment/opening-funding breakdown and a shared `ActionForm`. Confirmation sends
the owner fingerprint plus an actor-bound, expiring signed token; the command
owner rechecks classification, funding, locking, and idempotency before one
atomic transition. Templates do not infer eligibility from account balance.

#### Invoice Form (New)

```python
{
    "tax_rates": list[TaxRate],
    "tax_rates_json": list[dict],               # [{id, name, rate}] for Alpine.js
    "invoice_config": {                         # Alpine.js x-data payload
        "accountId": str,
        "invoiceNumber": str,
        "status": str,
        "currency": str,
        "issuedAt": str,                        # "YYYY-MM-DD"
        "dueAt": str,
        "memo": str,
        "taxRates": list[dict],
        "lineItems": list[dict],
        "invoiceId": str,
        "paymentTermsDays": int,
    },
    "default_issue_date": str,
    "default_due_date": str,
    "default_currency": str,
    "account_locked": bool,
    "account_label": str | None,
    "selected_account_id": str | None,
}
```

#### AR Aging Report

```python
{
    "buckets": {                                # Aging buckets
        "current": list[Invoice],
        "1_30": list[Invoice],
        "31_60": list[Invoice],
        "61_90": list[Invoice],
        "90_plus": list[Invoice],
    },
    "totals": {"current": float, "1_30": float, ...},
    "counts": {"current": int, "1_30": int, ...},
    "bucket_rows": {                            # Enriched rows
        "current": [{"invoice": Invoice, "account_label": str, "last_payment_at": date | None}],
    },
    "bucket_order": list[dict],                 # [{key, label, amount, count, is_selected}]
    "top_debtors": list[dict],                  # [{account_id, account_label, amount}]
    "aging_trend": {                            # Chart data
        "labels": list[str],                    # ["Jan 2024", ...]
        "series": {"current": list[float], "1_30": list[float], ...},
    },
}
```

---

### Billing — Payments

The filtered payment CSV at `GET /admin/billing/payments/export.csv` uses the
same uncapped filter and stable-sort scope as the payments list. Its customer
identity column is `customer_name`, populated from the canonical customer
account display name; internal account UUIDs are not exported. Rows stream
incrementally so large reconciliation exports are not held entirely in memory.

#### `GET /admin/billing/payments`
**Template:** `admin/billing/payments/index.html`

```python
{
    "payments": list[Payment],                  # enriched with display_number, display_method, narration

    "status_totals": {
        "succeeded":          {"count": int, "amount": float},
        "pending":            {"count": int, "amount": float},
        "failed":             {"count": int, "amount": float},
        "refunded":           {"count": int, "amount": float},
        "partially_refunded": {"count": int, "amount": float},
        "canceled":           {"count": int, "amount": float},
        "all":                {"count": int, "amount": float},
    },

    # ── Pagination ──
    "list_query": ListQuery,
    "page_meta": PageMeta,
    "page": int,
    "per_page": int,
    "total": int,
    "total_pages": int,
    "total_balance": float,
    "active_count": int,
    "suspended_count": int,

    # ── Filters ──
    "customer_ref": str | None,
    "selected_partner_id": str | None,
    "partner_options": list[dict],              # [{id, name}]
    "status": str | None,
    "method": str | None,
    "search": str | None,
    "start_date": str | None,                  # inclusive YYYY-MM-DD, UTC
    "end_date": str | None,                    # inclusive YYYY-MM-DD, UTC
    "unallocated_only": bool,

    "active_page": "payments",
}
```

#### Payment Form (New)

```python
{
    "prefill": {
        "invoice_id": str | None,
        "invoice_number": str | None,
        "amount": float | None,
        "currency": str | None,
        "status": str | None,                   # "succeeded"
        "account_id": str | None,
    },
    "selected_account": Subscriber | None,
    "invoice_label": str | None,
    "balance_value": str | None,
    "balance_display": str | None,
    "collection_accounts": list[CollectionAccount],
    "invoices": list[Invoice],                  # open invoices only
}
```

### Billing — Service extension detail

#### `GET /admin/billing/service-extensions/{extension_id}`
**Template:** `admin/billing/service_extension_detail.html`

Page contract:

- Screen: admin service-extension detail.
- Audience: billing and operations staff.
- Job: verify scope, ownership, lifecycle history, impact, and the available
  transition.
- Read owner: `ui.service_extension_detail_projection`.
- Command owner: `financial.service_extensions`.
- Primary entity: service-extension UUID.
- Activity depth: L3 investigation. Raw audit evidence and metadata remain L4
  and require `audit:read`.

The route supplies one typed `ServiceExtensionDetailProjection`. Its summary
includes server-owned `status_presentation`, created actor and application-
timezone timestamp, scope, days, outage window, and UUID. The decision area
receives owner-computed impact and permission-aware `can_apply`/`can_cancel`.
Users with only `billing:extension:read` can see summarized activity but no
action controls.

“Recent activity” appears below the decision/action area and before affected-
subscription evidence. It uses typed `ServiceExtensionActivityItem` values and
the shared `timeline_item` macro. Each item retains its action, safe actor label,
application-timezone timestamp, concise result, semantic tone, and explicit
legacy provenance when reconstructed. Newest-first order is deterministic.

The projection queries only exact `entity_type=service_extension` and exact
entity UUID audit evidence. It may reconstruct historical creation and apply
items from reliable legacy fields, de-duplicates them when canonical events
exist, and never invents a cancellation timestamp. There is no “View all” link
until the audit UI supports an exact entity-ID filter.

The responsive timeline keeps action, actor, and timestamp visible without a
horizontal table dependency. Status always includes text/icon semantics and is
never communicated by color alone. The template must not contain a local status
map, permission check, lifecycle transition rule, actor lookup, or timestamp
conversion.

#### `GET /admin/reports/tax`
**Template:** `admin/reports/tax.html`

`app.services.tax_accounting` owns this report contract. Output tax is tax on
issued invoice documents less issued credit-note tax adjustments, not collected
cash. WHT is shown as a separate receivable projection, and all aggregates remain
separated by currency.

```python
{
    "report_basis": "output_tax_invoiced_less_credit_note_adjustments_and_wht_receivable",
    "date_from": str,
    "date_to": str,
    "invoice_rows": list[dict],                # canonical tax/gross fields
    "output_tax_totals": list[dict],           # one aggregate per currency
    "output_tax_invoice_count": int,
    "output_tax_rows_truncated": bool,
    "credit_note_rows": list[dict],
    "credit_note_tax_totals": list[dict],
    "credit_note_count": int,
    "credit_note_rows_truncated": bool,
    "net_output_tax_totals": list[dict],       # invoiced - credit adjustments
    "wht_rows": list[dict],
    "wht_totals": list[dict],                  # gross/net/WHT/outstanding
    "wht_record_count": int,
    "wht_rows_truncated": bool,
}
```

Templates must not sum currencies, reconstruct outstanding WHT from raw proof
rows, omit issued credit-note adjustments, or label output tax as tax collected.

The report deliberately remains the canonical source-document tax-register
projection. Dotmac ERP owns account mappings, balanced journals, tax
transactions, and financial statements; neither side re-derives the other's
owned state.
Credit-note period recognition uses the first owner-set `issued_at` timestamp;
draft creation time is not a tax point. Automated cancellation credits retain
the source invoice line's configured rate and inclusive/exclusive/exempt mode.

#### `GET /admin/billing/tax-accounting`
**Template:** `admin/billing/tax_accounting.html`

The tax owner supplies a bounded WHT evidence queue. POST actions request legal
WHT transitions through the owner; routes do not write models directly. WHT
certification requires certificate evidence, write-off requires a reason, and
reclaimed/written-off states are terminal. Every transition is appended to the
official timeline and advances the payment sync watermark for ERP.
WHT lifecycle labels, tones, and icons are server-owned `StatusPresentation`
values rendered through the shared semantic status macro.

Page contract: this is the Sub finance-admin source-fact plane for moving WHT
receivables through evidence-backed transitions. `financial.tax_accounting`
owns both the read/context contract and commands; billing tax RBAC owns access.
The WHT queue has server-side status/search filtering, newest-first ordering,
counts, and pagination. Search covers reseller name, direct-customer identity,
record ID, customer/billing account ID, and certificate reference. Rows may
represent either a direct customer account or a consolidated billing account.
Account mapping and journal operations belong in Dotmac ERP and are not
presented here.

For invoice-linked direct bank-transfer payments where the customer WHT policy
is enabled, the customer portal receives a read-only server-owned snapshot with
invoice total, VAT-exclusive WHT basis, VAT amount, configured WHT percentage,
WHT deduction, exact bank-transfer amount, gross credit after verification, and
the certificate amount that remains outstanding. Templates and clients must not
ask the customer for a WHT rate, must not recalculate VAT-exclusive basis from
gross or net values, and must not offer automatic WHT for arbitrary
account-credit deposits or online/card checkout. When WHT is disabled or the
payment method is not direct bank transfer, the portal preserves the existing
full-value payment experience.

---

### Catalog — Offers

#### `GET /admin/catalog/offers`
**Template:** `admin/catalog/offers/index.html`

```python
{
    "offers": list[CatalogOffer],

    "offer_subscription_counts": dict[str, int],        # offer_id → total count
    "offer_active_subscription_counts": dict[str, int], # offer_id → active count
    "offer_plan_metadata": dict[str, dict],             # offer_id → {plan_kind, ip_block_size}

    # ── Enum Lists (for filter dropdowns & forms) ──
    "service_types": list[str],
    "access_types": list[str],
    "price_bases": list[str],
    "billing_cycles": list[str],
    "contract_terms": list[str],
    "offer_statuses": list[str],
    "plan_categories": list[str],
    "radius_profiles": list[RadiusProfile],

    # ── Filters ──
    "status": str | None,
    "plan_kind": str,
    "plan_category": str,
    "search": str | None,

    # ── Pagination ──
    "page": int,
    "per_page": int,
    "total": int,
    "total_pages": int,

    "active_page": "catalog",
}
```

#### Offer Form (Create/Edit)

```python
{
    "offer": dict,                              # form data (existing values or defaults)

    # ── Dropdown Options ──
    "region_zones": list[RegionZone],
    "all_offers": list[CatalogOffer],
    "usage_allowances": list[UsageAllowance],
    "sla_profiles": list[SlaProfile],
    "radius_profiles": list[RadiusProfile],
    "policy_sets": list[PolicySet],
    "add_ons": list[AddOn],
    "addon_links_map": dict[str, dict],         # addon_id → {is_required, min_quantity, max_quantity}

    # ── Enum Options ──
    "service_types": list[str],
    "access_types": list[str],
    "price_bases": list[str],
    "billing_cycles": list[str],
    "billing_modes": list[str],
    "contract_terms": list[str],
    "offer_statuses": list[str],
    "price_units": list[str],
    "price_types": list[str],                   # ["recurring", "one_time"]
    "guaranteed_speed_types": list[str],
    "plan_categories": list[str],
    "plan_kinds": list[str],                    # ["standard", "ip_address", "device_replacement"]
    "ip_block_sizes": list[str],                # ["/32", "/30", ...]

    "action_url": str,
    "error": str | None,
}
```

#### Plan Usage Chart Data (HTMX)

```python
{
    "labels": list[str],                        # date labels
    "total_counts": list[int],
    "active_counts": list[int],
    "total_now": int,
    "active_now": int,
    "max_total": int,
    "avg_total": float,
    "period": str,                              # "daily" | "weekly" | "monthly"
}
```

---

### Catalog — Subscriptions

#### `GET /admin/catalog/subscriptions`
**Template:** `admin/catalog/subscriptions/index.html`

```python
{
    "subscriptions": list[Subscription],
    "offers": list[CatalogOffer],               # active only (for filter dropdown)
    "status": str | None,
    "page": int,
    "per_page": int,
    "total": int,
    "total_pages": int,
    "active_page": "catalog",
}
```

#### `GET /admin/catalog/subscriptions/{id}`

Mistaken duplicate activations are projected as `correction_action_forms`, one
shared `ActionForm` per owner-reviewed restorable sibling. The action is absent
without `catalog:write`. Each eligible form carries the exact target,
owner-produced preview fingerprint, and scoped idempotency key; it discloses the
lifecycle, PPPoE/RADIUS profile, FUP, and no-automatic-financial-adjustment
consequences before requiring confirmation. Its title distinguishes correction
from ordinary Restore, and its description and impact show the full target UUID
and creation timestamp so repeated plan names cannot be confused. Ineligible
forms remain visible to authorized operators with the owner-provided blocker,
including malformed IP evidence, PPPoE identity mismatch, target locks, and
unconfigured target speed. The template only renders
`action_form(correction_action)` and never derives eligibility or fingerprints.

The generic Restore preview rejects a restorable subscription when another
active sibling uses the same login. That is an atomic-correction case, not a
single-service lifecycle action.

Exact-service IPv4 projection drift is projected as
`ipv4_projection_reconciliation_action`, a shared `ActionForm` visible only to
`catalog:write` operators and only when there is actionable or blocked drift.
The form shows the IPAM-owned, served, and observed RADIUS addresses. When the
owner preview returns `ready`, it carries the exact assignment identifier,
preview fingerprint, and per-render idempotency key; it discloses old-session
reauthentication and the absence of billing, plan, add-on, or service-period
changes before requiring confirmation. Missing/ambiguous assignment, inactive
service, shared login, unavailable/divergent RADIUS, and conflicting-session
states remain visible with the owner-derived blocker. An aligned `noop` action
is omitted. The template renders the shared action form and never derives
eligibility or fingerprints.

#### Subscription Form

```python
{
    "subscription": dict,                       # form data

    # ── Related Records ──
    "accounts": list[SubscriberAccount],
    "offers": list[CatalogOffer],
    "nas_devices": list[NasDevice],
    "router_devices": list[NasDevice],
    "ipv4_pools": list[IpPool],
    "ipv4_blocks": list[dict],                  # [{id, pool_id, pool_name, cidr, available_count, available_ips, display}]
    "radius_profiles": list[RadiusProfile],

    # ── Enum Options ──
    "subscription_statuses": list[str],
    "billing_modes": list[str],
    "contract_terms": list[str],

    # ── Display Helpers ──
    "subscriber_label": str,
    "selected_router_label": str,
    "current_service_login": str,
    "current_service_password": str,
    "credential_targets": {
        "email": list[str],
        "sms": list[str],
    },
    "billing_mode_help_text": str,
    "billing_mode_prepaid_notice": str,
    "billing_mode_postpaid_notice": str,

    "action_url": str,
    "error": str | None,
}
```

---

### Network — OLTs

#### `GET /admin/network/olts`
**Template:** `admin/network/olts/index.html`

OLT listing uses the core device listing context — devices filtered to OLT type. Key fields per device:

```python
# Each OLT in the list:
{
    "id": str (UUID),
    "name": str,
    "hostname": str | None,
    "vendor": str | None,
    "model": str | None,
    "mgmt_ip": str | None,
    "status": DeviceStatus,                     # active | inactive | maintenance | retired
    "firmware_version": str | None,
    "snmp_enabled": bool,
    "created_at": datetime,
    # Relationships:
    "pon_ports": list[PonPort],
}
```

#### OLT TR-069 Profiles Context

```python
(
    ok: bool,
    message: str,
    profiles_data: list[dict],                  # [{profile_id, name, acs_url, acs_username, inform_interval, binding_count}]
    {
        "acs_prefill": {acs_url, acs_username, acs_name},
        "onts": list[dict],                     # [{id, serial_number, board, port, onu_index, name, online, subscriber_name}]
    }
)
```

---

### Network — ONTs

#### ONT Form Dependencies

```python
{
    "onu_types": list,
    "olt_devices": list[OLTDevice],
    "vlans": list[Vlan],
    "zones": list,
    "splitters": list[Splitter],
    "speed_profiles_download": list[SpeedProfile],
    "speed_profiles_upload": list[SpeedProfile],
    "pon_types": list[str],                     # PonType enum values
}
```

#### ONT Bulk Action Result

```python
{
    "succeeded": int,
    "failed": int,
    "skipped": int,
    "total": int,
    "results": list[dict],                      # [{ont_id, success, message}]
}
```

---

### Provisioning

#### `GET /admin/provisioning/bulk-activate`
**Template:** `admin/provisioning/bulk_activate.html`

```python
{
    # ── Tab & Filter Options ──
    "tabs": list[str],                          # ["internet", "recurring", "bundle"]
    "tab": str,
    "offers": list[CatalogOffer],
    "resellers": list[Reseller],
    "pop_sites": list[PopSite],
    "nas_devices": list[NasDevice],
    "subscriber_statuses": list[str],
    "jobs": list[dict],                         # recent job history

    "active_page": "provisioning",
}
```

#### Bulk Activate Preview (HTMX)

```python
{
    "rows": list[dict],                         # Each:
    #   {
    #       "subscriber_id": str,
    #       "subscriber_name": str,
    #       "subscriber_email": str,
    #       "subscriber_status": str,
    #       "existing_subscription_id": str,
    #       "existing_offer_name": str,
    #       "existing_subscription_status": str,
    #       "action": "create" | "update" | "skip_active_exists",
    #       "reason": str,
    #   }
    "total_matches": int,
    "shown": int,
    "counts": {"create": int, "update": int, "skip": int},
}
```

#### Job Status

```python
{
    "job_id": str,
    "status": "queued" | "running" | "completed" | "partial" | "failed",
    "progress_percent": int,                    # 0-100
    "queued_at": str,                           # ISO datetime
    "started_at": str | None,
    "completed_at": str | None,
    "error": str | None,
    "result": dict | None,
    "counts": {"activated": int, "failed": int, "skipped": int},
    "total_matches": int,
}
```

---

### Reports

#### `GET /admin/reports`
**Template:** `admin/reports/index.html`

Tabs: Network, Revenue, Subscribers, Churn, Technician

#### Network Report

```python
{
    "olts": list[OLT],
    "total_olts": int,
    "active_olts": int,
    "total_onts": int,
    "connected_onts": int,
    "recent_ont_activity": list[ONT],
    "pool_data": list[dict],                    # [{name, cidr, used_count, total_count}]
    "used_ips": int,
    "total_ips": int,
    "ip_pool_usage": float,                     # percentage
    "active_vlans": int,
}
```

#### Revenue Report

```python
{
    "total_revenue": Decimal,
    "revenue_growth": float,
    "recurring_revenue": Decimal,
    "outstanding_amount": Decimal,
    "outstanding_count": int,
    "collection_rate": float,                   # percentage
    "recent_payments": list[Payment],
}
```

#### Subscribers Report

```python
{
    "total_subscribers": int,
    "subscriber_growth": float,
    "new_this_month": int,
    "active_subscribers": int,
    "suspended_subscribers": int,
    "active_rate": float,
    "status_breakdown": dict[str, int],         # {status_name: count}
    "recent_subscribers": list[Subscriber],
}
```

#### Churn Report

```python
{
    "churn_rate": float,
    "retention_rate": float,
    "cancelled_count": int,
    "at_risk_count": int,
    "churn_reasons": {"price": int, "service_quality": int, "moved": int, "competitor": int},
    "recent_cancellations": list[Subscriber],
}
```

---

### VPN Management

#### `GET /admin/vpn`
**Template:** `admin/vpn/index.html`

```python
{
    # ── Unified Dashboard ──
    "wireguard": dict,                          # from web_vpn_servers
    "openvpn_clients": list[dict],              # [{id, name, client_ip, is_connected, connected_since, rx_bytes, tx_bytes, latency_ms}]
    "openvpn_config": dict,                     # {remote_host, remote_port, proto, server_subnet}

    "connections": list[dict],                  # Unified view:
    #   {
    #       "protocol": "wireguard" | "openvpn",
    #       "id": str,
    #       "name": str,
    #       "server_name": str,
    #       "status": "up" | "down",
    #       "last_handshake_at": datetime | None,
    #       "uptime_seconds": int | None,
    #       "rx_bytes": int,
    #       "tx_bytes": int,
    #       "latency_ms": int | None,
    #       "address": str,
    #   }

    "summary": {
        "total": int,
        "active": int,
        "down": int,
        "wireguard": int,
        "openvpn": int,
    },

    "alerts": list[dict],                       # [{id, protocol, tunnel, severity, message, created_at}]

    "active_page": "vpn",
}
```

---

### Resellers

#### `GET /admin/resellers/{id}`
**Template:** `admin/resellers/detail.html`

```python
{
    "reseller": Reseller,                       # ORM object
    "reseller_subscribers": list[ResellerUser], # paginated
    "reseller_subscribers_total": int,
    "page": int,
    "per_page": int,
    "total_pages": int,
    "active_page": "resellers",
}
```

---

### Notifications

#### `GET /admin/notifications` (Dropdown partial)
**Template:** `admin/notifications/_menu.html`

```python
{
    "notifications": list[Notification],        # max 10, ORM objects
}
```

---

### System / Admin

System has 20+ web services covering: Users, Roles, Permissions, API Keys, Settings, Audit Logs, Health, Logs, Company Info, Scheduler, Webhooks, Import/Export, DB Inspector.

Each follows the same pattern — context dict with list data, form helpers, and enum options. Key entry points:

| URL | Purpose | Key Context |
|-----|---------|-------------|
| `/admin/system/users` | User list | users, roles, page, total |
| `/admin/system/users/{id}` | User detail | user, roles, permissions, activity |
| `/admin/system/roles` | Role list | roles, permission_groups |
| `/admin/system/settings` | Settings hub | domains, settings_by_domain |
| `/admin/settings/billing/collection-accounts` | Receiving accounts and customer transfer destinations | accounts, account_types, show_inactive |
| `/admin/settings/billing/payment-channels` | Recorded settlement-channel attribution | channels, providers, channel_types, show_inactive |
| `/admin/settings/billing/payment-channel-accounts` | Channel-to-collection-account attribution | mappings, channels, collection_accounts, show_inactive |
| `/admin/system/audit` | Audit log | events, filters, page |
| `/admin/system/health` | System health | cpu, memory, disk, services |

The role editor submits role attributes and the complete visible permission
policy to `auth.rbac_catalog` in one atomic command. Saving permissions preserves
an unchanged legacy role name; only a genuine rename must satisfy the canonical
lowercase identifier syntax.

---

## Model Field Reference

These are the ORM objects your templates receive. Access fields with dot notation: `{{ invoice.total }}`.

### Subscriber

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | Internal, use for URLs |
| `first_name` | str | |
| `last_name` | str | |
| `display_name` | str \| None | Preferred over first+last when set |
| `email` | str | |
| `phone` | str \| None | E.164 format |
| `status` | SubscriberStatus | Enum: new, active, suspended, disabled, canceled, delinquent |
| `subscriber_number` | str \| None | Human-readable ID |
| `account_number` | str \| None | |
| `is_active` | bool | |
| `billing_enabled` | bool | |
| `billing_day` | int \| None | Day of month (1-28) |
| `mrr_total` | Decimal \| None | Monthly recurring revenue — `tabular-nums font-mono` |
| `address_line1` | str \| None | |
| `city` | str \| None | |
| `region` | str \| None | |
| `country_code` | str \| None | 2-char ISO |
| `created_at` | datetime | Relative for < 7 days |
| `updated_at` | datetime | |
| `.subscriptions` | list[Subscription] | Relationship |
| `.organization` | Organization \| None | Relationship |

### Invoice

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `invoice_number` | str \| None | Human-readable |
| `status` | InvoiceStatus | Enum: draft, issued, partially_paid, paid, void, overdue, written_off. Terminal financial states are read-only outside owner preview/confirmation flows. |
| `currency` | str | 3-char, default "NGN" |
| `subtotal` | Decimal | `tabular-nums font-mono` |
| `tax_total` | Decimal | |
| `total` | Decimal | |
| `balance_due` | Decimal | |
| `issued_at` | datetime \| None | |
| `due_at` | datetime \| None | |
| `paid_at` | datetime \| None | |
| `memo` | str \| None | |
| `is_proforma` | bool | |
| `.account` | Subscriber | The billed subscriber |
| `.lines` | list[InvoiceLine] | Line items |

### Payment

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `amount` | Decimal | `tabular-nums font-mono` |
| `currency` | str | |
| `status` | PaymentStatus | Enum: pending, succeeded, failed, refunded, partially_refunded, canceled |
| `paid_at` | datetime \| None | |
| `receipt_number` | str \| None | |
| `external_id` | str \| None | Provider reference |
| `memo` | str \| None | |
| `.account` | Subscriber | |
| `.payment_method` | PaymentMethod \| None | |

### CatalogOffer

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `name` | str | |
| `code` | str \| None | Short code |
| `service_type` | ServiceType | Enum: residential, business |
| `plan_category` | PlanCategory | Enum: internet, recurring, one_time, bundle |
| `billing_cycle` | BillingCycle | Enum: daily, weekly, monthly, annual |
| `speed_download_mbps` | int \| None | Display as "100 Mbps" |
| `speed_upload_mbps` | int \| None | Display as "50 Mbps" |
| `status` | OfferStatus | Enum: active, inactive, archived |
| `.prices` | list[OfferPrice] | Pricing tiers |
| `.subscriptions` | list[Subscription] | |

### Subscription

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `status` | SubscriptionStatus | Enum: pending, active, suspended, stopped, disabled, archived, canceled, expired |
| `billing_mode` | BillingMode | Enum: prepaid, postpaid |
| `contract_term` | ContractTerm | Enum: month_to_month, twelve_month, twentyfour_month |
| `login` | str \| None | PPPoE username — `font-mono` |
| `ipv4_address` | str \| None | `font-mono` |
| `mac_address` | str \| None | `font-mono` |
| `start_at` | datetime \| None | |
| `end_at` | datetime \| None | |
| `.subscriber` | Subscriber | |
| `.offer` | CatalogOffer | |

### OLT Device

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `name` | str | |
| `hostname` | str \| None | |
| `vendor` | str \| None | |
| `model` | str \| None | |
| `mgmt_ip` | str \| None | `font-mono` |
| `firmware_version` | str \| None | |
| `status` | DeviceStatus | Enum: active, inactive, maintenance, retired |
| `snmp_enabled` | bool | |
| `.pon_ports` | list[PonPort] | |

### ServiceOrder

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `status` | ServiceOrderStatus | Enum: draft, submitted, scheduled, provisioning, active, canceled, failed |
| `order_type` | ServiceOrderType \| None | Enum: new_install, upgrade, downgrade, disconnect, reconnect, change_service |
| `notes` | str \| None | |
| `.subscriber` | Subscriber | |
| `.subscription` | Subscription \| None | |

---

## HTMX Interaction Map

### Auto-Refresh Partials (Polling)

| Trigger Element | Endpoint | Target | Interval |
|----------------|----------|--------|----------|
| Dashboard stats cards | `GET /admin/dashboard/stats` | `#stats-container` | 30s |
| Activity feed | `GET /admin/dashboard/activity` | `#activity-feed` | 30s |
| Server health widget | `GET /admin/dashboard/server-health` | `#server-health` | 60s |

### Search (Live Filtering)

| Page | Endpoint | Trigger | Target |
|------|----------|---------|--------|
| Customers | `GET /admin/customers?search=...` | `keyup changed delay:300ms` | `#data-table` |
| Subscribers | `GET /admin/subscribers?search=...` | `keyup changed delay:300ms` | `#data-table` |
| Invoices | `GET /admin/billing?search=...` | `keyup changed delay:300ms` | `#data-table` |
| Offers | `GET /admin/catalog/offers?search=...` | `keyup changed delay:300ms` | `#data-table` |

### Dynamic Form Fields

| Trigger | Endpoint | Updates |
|---------|----------|---------|
| Account select (payment form) | `GET /admin/billing/payments/invoice-options?account_id=...` | Invoice dropdown |
| Invoice select (payment form) | `GET /admin/billing/payments/invoice-details?invoice_id=...` | Amount, currency |
| Status filter click (invoices) | `GET /admin/billing?status=...` | Table rows |
| Tab switch (offers) | `GET /admin/catalog/offers?plan_category=...` | Table rows |
| Bulk activate preview | `POST /admin/provisioning/bulk-activate/preview` | Preview table |

### Toast Notifications

Triggered via `HX-Trigger` response header:
```python
# In web service:
response.headers["HX-Trigger"] = json.dumps({
    "showToast": {"message": "Invoice created", "type": "success"}
})
```

Template listens:
```html
<div x-data @show-toast.window="...">
```

---

## Form Patterns

### Server-owned action forms

High-impact action contracts may carry owner-produced hidden values such as a
state-preview fingerprint. The shared renderer emits those values unchanged and
uses a required, labeled confirmation checkbox when confirmation is declared.
Do not use `window.confirm`, `onclick`/`onsubmit` confirmation handlers, or
template-generated fingerprints. The command owner must recheck the fingerprint
and confirmation under its transaction lock.

High-impact and lifecycle action forms use
`app.services.action_forms.ActionForm` and the shared
`components/forms/action_form.html` renderer. The resource projection composes
RBAC visibility with eligibility and typed errors from the command owner. It
declares impact, confirmation, fields/options, defaults, and submitted values;
templates do not branch on raw state or hand-map domain failures.

The first adopted resource is payment-proof review. Its contract is built by
`app.services.web_billing_payment_proofs`, while
`app.services.payment_proofs` remains authoritative for review eligibility,
duplicate-reference policy, payment/WHT consequences, validation, locking, and
execution. Unauthorized verify/reject forms are absent. An unavailable verify
action may remain visible with the owner-provided duplicate reason when reject
is still valid.

Payment-arrangement review and dunning-case staff actions use the same
server-owned form contract. Dunning bulk actions always preview an explicit set
of case identifiers, disclose every eligible and skipped case, bind that exact
scope and current owner state into a fingerprint, and revalidate it while the
cases are locked. Templates must not infer transition eligibility or submit
page-filter criteria as an implicit bulk scope.

Invoice batch launch/retry and invoice bulk actions also use server-rendered
review forms. A manual batch preview lists exact postpaid subscription
membership and carries its current fingerprint; a retry is available only from
a failed run and produces a new linked run. Invoice list and AR-aging actions
submit explicit invoice IDs to a review page and carry the owner-produced
resolved count and scope token. Client JavaScript may collect visible-page IDs,
but it does not display confirmation dialogs or execute the financial command.

Failed submissions re-render the detail page with the operator's declared
values and `aria-invalid` field errors or one `role="alert"` general error.
Successful submissions use POST-Redirect-GET. Financial, destructive, or
customer-visible action contracts show impact and require explicit confirmation.
Concrete action colors resolve through branding-owned semantic role tokens.

### CSRF Token (Required on every POST form)
```html
<form method="POST" action="/admin/billing/invoices/create">
    {{ csrf_token_field | safe }}
    <!-- fields -->
</form>
```

### POST-Redirect-GET
All form submissions redirect after success:
```
POST /admin/customers/new → 303 → GET /admin/customers/{type}/{id}
POST /admin/billing/invoices/create → 303 → GET /admin/billing
POST /admin/catalog/offers/create → 303 → GET /admin/catalog/offers
```

### Context Pre-fill via Query Parameters
Forms accept URL params to pre-populate:
```
/admin/billing/invoices/new?account_id=xxx     → locks account selector
/admin/billing/payments/new?invoice_id=xxx     → locks invoice, pre-fills amount
/admin/catalog/subscriptions/new?subscriber_id=xxx
```

When pre-filled:
- Show field as read-only with visual indicator
- Include as hidden `<input>` for submission
- Display context banner explaining the pre-fill

### Alpine.js Form State

Complex forms use Alpine.js `x-data` with JSON config from the server:

```html
<!-- CRITICAL: use single quotes for x-data with tojson -->
<div x-data='{{ invoice_config | tojson }}'>
    <input x-model="invoiceNumber" />
    <template x-for="(item, i) in lineItems">
        <!-- dynamic line items -->
    </template>
</div>
```

### Validation Errors
- Re-render form with all values preserved
- Display errors inline next to fields
- Flash message via `HX-Trigger` for general errors

---

## Template Conventions

### None/Empty Handling
```html
{{ value or '' }}
{{ value or 'N/A' }}
{% if value %}{{ value }}{% endif %}
```

### Domain status display

For account, subscription, invoice, payment, outage-incident, device operational,
customer connection-health, support-ticket, and field work-order state, project through
`app.services.status_presentation` and pass the returned object to
`status_presentation_badge`. Do not read `.value` to select a label, tone,
icon, or Tailwind class in a template. Shared renderers resolve semantic tones
only through the branding-generated `status-*` theme classes; literal
tone-to-color lookups and dynamic Tailwind interpolation remain prohibited.

Other domains should name their own presentation owner before adding repeated
status dictionaries. A one-off renderer must still use static, purge-safe class
lookups and include a non-color distinction.

### Data Formatting

`app.services.display_format` is the code-native owner for currency-code
normalization, money display, multi-currency summary ordering, configured
display timezone, and timestamp strings. Web projection services provide the
formatted value; migrated templates do not call Python number/date formatting
or invent NGN/Africa-Lagos defaults.

| Type | Owner-provided projection | Example |
|------|---------------------------|---------|
| Single-currency money | `display_format.format_money(amount, currency=code)` | `₦5,000.00` |
| Mixed-currency total | `display_format.format_currency_groups(amounts)` | `NGN 5,000.00, USD 25.00` |
| Timestamp | `display_format.format_timestamp(value, db)` | `2026-07-14 15:30 WAT` |
| Missing scalar fact | Owner-provided missing state | `—` |
| Bandwidth | Declared value plus declared unit | `100 Mbps` |
| IP address | Owner-provided identifier, rendered `font-mono` | `192.168.1.1` |
| MAC address | Owner-provided identifier, rendered `font-mono` | `AA:BB:CC:DD:EE:FF` |
| Percentage | Owner-provided precision plus explicit `%` unit | `95.5%` |

Domain services own the raw amount, ISO currency, unit, timestamp, and
availability facts. Formatting must not convert currencies, sum unlike
currencies, or turn unknown/stale/unavailable values into zero. Empty aggregates
may explicitly project zero; missing scalar facts use the shared em-dash marker.
The mobile `Fmt` helper is a platform renderer and
must consume the same declared facts instead of becoming a parallel owner.

### Safe Filter Rules

`| safe` is ONLY allowed for:
- `{{ csrf_token_field | safe }}` — CSRF token
- `{{ data | tojson }}` — JSON for Alpine.js
- Icon macro output — `{{ icon_users() | safe }}` (when passed to other macros)
- Admin-authored CSS

**NEVER** use `| safe` on user-submitted content (XSS risk).

---

## File Structure Reference

```
templates/
├── layouts/
│   ├── admin.html              ← Admin portal base (sidebar + nav)
│   ├── customer.html           ← Customer portal base
│   ├── reseller.html           ← Reseller portal base
│   └── base.html               ← Root base (head, scripts, CSS)
├── admin/
│   ├── dashboard/
│   │   ├── index.html
│   │   ├── _stats.html         ← HTMX partial
│   │   ├── _activity.html      ← HTMX partial
│   │   └── _server_health.html ← HTMX partial
│   ├── customers/
│   │   ├── index.html
│   │   ├── detail.html
│   │   ├── new.html
│   │   └── _table.html         ← HTMX partial
│   ├── subscribers/
│   │   ├── index.html
│   │   └── detail.html
│   ├── billing/
│   │   ├── index.html          ← Invoice list
│   │   ├── invoices/
│   │   │   ├── detail.html
│   │   │   ├── new.html
│   │   │   └── edit.html
│   │   ├── payments/
│   │   │   ├── index.html
│   │   │   ├── new.html
│   │   │   └── detail.html
│   │   └── ...
│   ├── catalog/
│   │   ├── offers/
│   │   │   ├── index.html
│   │   │   ├── new.html
│   │   │   └── detail.html
│   │   └── subscriptions/
│   ├── network/
│   │   ├── olts/
│   │   ├── onts/
│   │   ├── devices/
│   │   └── ...
│   ├── provisioning/
│   ├── reports/
│   ├── system/
│   └── errors/
│       ├── 404.html
│       └── 500.html
├── components/
│   ├── ui/
│   │   └── macros.html         ← 40+ reusable macros
│   └── navigation/
├── auth/
├── customer/
├── reseller/
└── public/
```

---

## Quick Start for Frontend Dev

1. **Read `templates/layouts/admin.html`** — understand the shell
2. **Read `templates/components/ui/macros.html`** — your component library
3. **Pick a module** (e.g., Billing) and read its existing template
4. **Check the context spec above** for what data is available
5. **Use macros** — don't rebuild buttons, tables, badges from scratch
6. **Always pair dark mode** — `bg-white dark:bg-slate-800`
7. **Use branding/theme tokens** — `primary-*`/`accent-*` for identity and
   `status-*` roles for state; do not add literal module or status colors
8. **Test with HTMX** — partials return fragments, not full pages
