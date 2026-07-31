# Section 4: legacy BSS Administration

> **Status: historical product research, not an approved requirement.** Revalidate all UI proposals against `docs/UI_INFORMATION_AND_ACTION_STANDARD.md` and the current control-plane SOT.

## Source: legacy BSS ISP Management Platform

This document captures feature observations from 42 screenshots of legacy BSS's Administration module and proposes corresponding feature improvements for DotMac Sub. Each screenshot has been reviewed and organized into logical subsections.

---

## 4.1 Administration Dashboard

**Screenshot reviewed:** `Administration Dashboard.png`

**legacy BSS features observed:** A centralized administration hub organized into four sections -- (1) legacy BSS core management (Administrators, Roles, Partners, Locations, API Keys), (2) Logs (Operations, Internal, Portal, Files, Email, SMS, Sessions, API, Customer/Service status changes, Accounting integrations, Mailjet/Paystack logs, Planned changes), (3) Information (License, Site, Documentation, Forum, API docs), and (4) Reports (20+ report types covering internet usage, finance, customers, invoices, agents, resellers, services, tickets, DNS threats, and more). Each item is represented as a clickable link with a colored icon and clear label. A search bar allows quick filtering of admin functions.

**Proposed improvements for DotMac Sub:**

- [ ] Add a unified Administration hub page at `/admin/system` that groups all system functions into categorized sections (Core, Logs, Information, Reports) with icon-labeled links
- [ ] Add a search/filter input on the administration hub to quickly locate admin functions by keyword
- [ ] Ensure every log viewer, report, and configuration tool is accessible from this single hub page rather than requiring sidebar navigation alone

---

## 4.2 Administrators Management

**Screenshot reviewed:** `Administrators.png`

**legacy BSS features observed:** A table listing all administrator accounts with columns for ID, Admin login, Full name, Photo (avatar), Role (Super administrator, Administrator, Partner), Partner assignment, Phone number, and row-level Actions (edit, impersonate, delete). Filterable by Partner dropdown. Supports table search and configurable entries per page (100).

**Proposed improvements for DotMac Sub:**

- [ ] Add avatar/photo upload support for admin user profiles displayed in the user list table
- [ ] Add a "Partner" or "Organization" filter dropdown on the users list page to scope by reseller/partner affiliation
- [ ] Display phone number column in the admin users table
- [ ] Add an "Impersonate" action button per admin user allowing super-admins to log in as another user for troubleshooting
- [ ] Support configurable page size (25/50/100) on the users list table

---

## 4.3 Roles Management

**Screenshot reviewed:** `Roles.png`

**legacy BSS features observed:** A comprehensive roles table with 16 predefined ISP-specific roles: administrator, customer-creator, engineer, finance, financial-manager, frontdesk, manager, noc, operations-support, partner, procurement, project, sales, super-administrator, technical-support, and technician. Each has a Name (slug) and Title (display name). Actions include edit, view members, and delete (with some roles being system-protected and not deletable).

**Proposed improvements for DotMac Sub:**

- [ ] Add ISP-specific predefined role templates: engineer, noc, frontdesk, operations-support, procurement, project, sales, technical-support, and technician
- [ ] Add a "View members" action on each role to quickly see which users are assigned to it
- [ ] Distinguish between system-protected roles (edit only) and custom roles (edit + delete) in the UI
- [ ] Add a display Title field separate from the role Name/slug for user-friendly labeling
- [ ] Add a "customer-creator" role with limited permissions scoped to subscriber creation only

---

## 4.4 Partners Management

**Screenshot reviewed:** `Partners.png`

**legacy BSS features observed:** A partners/resellers table showing 20+ partner organizations with columns for ID, Partner name, Customers count (clickable, showing totals like 4,996 for "Main"), Customers online (real-time count, e.g., 1,243), and row-level Actions (edit, delete). This provides a high-level overview of multi-tenant customer distribution across partner organizations.

**Proposed improvements for DotMac Sub:**

- [ ] Add real-time "Customers online" count to the resellers list page showing currently-connected subscriber sessions per partner
- [ ] Add clickable customer count on the resellers table that navigates to the filtered subscribers list for that partner
- [ ] Display per-partner summary statistics (total customers, online customers) as sortable columns on the reseller index page
- [ ] Add a partner/reseller dashboard card showing distribution of subscribers across all partners

---

## 4.5 Agents Report

**Screenshot reviewed:** `Agents.png`

**legacy BSS features observed:** A sales agent commission tracking system with three tabs: Agents consolidated report, Agent report, and Customer report. Columns include Agent name, Commissioned transactions count, Unpaid total, Total revenue, and Commission amount. Period date range selector for filtering. Includes a Totals summary row at the bottom.

**Proposed improvements for DotMac Sub:**

- [ ] Add a sales agent/commission tracking module with per-agent revenue attribution
- [ ] Create a consolidated agent commission report with period filtering (date range selector)
- [ ] Add an "Agent report" sub-view showing per-agent detail with commissioned transactions and unpaid totals
- [ ] Add a "Customer report" sub-view showing which customers were acquired by which agent
- [ ] Include a totals summary row in commission reports (total commissioned transactions, unpaid total, commission)
- [ ] Support commission calculation rules (percentage-based, flat-rate, tiered) configurable per agent

---

## 4.6 Locations Management

**Screenshot reviewed:** `Locations.png`

**legacy BSS features observed:** A locations table listing geographical regions (e.g., Abuja with 4,901 customers and 1,215 online, Lagos with 357 customers and 103 online). Columns: ID, Location name, Customers count, Customers online, Taxes (associated tax configuration, showing "Not used"), and Actions (edit, delete). Supports location-based tax assignment.

**Proposed improvements for DotMac Sub:**

- [ ] Add a Locations management page that defines geographical service regions with customer counts and real-time online counts
- [ ] Link tax rules/rates to specific locations for location-based tax calculation on invoices
- [ ] Display location-scoped subscriber statistics (total count, online count) on the locations management page
- [ ] Allow filtering subscribers, services, and billing reports by location

---

## 4.7 API Keys Management

**Screenshot reviewed:** `API Keys.png`

**legacy BSS features observed:** An API keys table listing 25+ named integrations (NetworkWeatherMap, legacy BSS Google Maps, legacy BSS Paystack Add-On, Google Maps, Social Registration Add-on, Network Weathermap, Speedtest, Mobile App, QuickBooks Accounting, legacy BSS Add-on Mobijet, Mikrotik discover/export add-on, legacy BSS Huawei OLT, legacy BSS Ruijie, Zoho books, Zoho CRM). Each key has an ID, Name, Key string (partially visible), Partner scope, and Actions. Filterable by Partner.

**Proposed improvements for DotMac Sub:**

- [ ] Add a dedicated API Keys management page at `/admin/system/api-keys` for creating and managing API access tokens
- [ ] Support named API keys with descriptive labels (e.g., "Paystack Integration", "Mobile App", "Monitoring System")
- [ ] Scope API keys to specific partner/organization for multi-tenant API access control
- [ ] Add API key usage tracking (last used timestamp, request count) visible in the keys table
- [ ] Support API key rotation with graceful deprecation period for old keys
- [ ] Add ability to restrict API key permissions to specific endpoints/operations

---

## 4.8 API Logs

**Screenshot reviewed:** `API.png`

**legacy BSS features observed:** An API request audit log with filter bar (Customer ID, Period date range, API key selector, Operation type filter). Table columns: Date and time, API key used, Operation performed, Result status, and Actions (view detail). Provides complete audit trail of all API interactions.

**Proposed improvements for DotMac Sub:**

- [ ] Add an API request audit log page at `/admin/system/audit/api` showing all API calls with timestamp, key used, operation, and result
- [ ] Add filter controls: customer ID lookup, date range, API key selector, and operation type dropdown
- [ ] Log API request/response details viewable from an expand/detail action
- [ ] Add rate limiting visibility -- show which API keys are approaching or exceeding rate limits
- [ ] Support CSV/JSON export of API audit logs for compliance

---

## 4.9 Operations Log

**Screenshot reviewed:** `Operations.png`

**legacy BSS features observed:** An administrator operations audit log recording all admin actions. Columns: Date and time, Administrator (who performed the action), Operation description (e.g., "View internet service", "Edit internet service", "View customer", "Create payment"), Customer ID, Result (Success/Failure). Filterable by Customer ID, Period, Administrator, and Operation type. Shows both system-initiated and human-initiated operations.

**Proposed improvements for DotMac Sub:**

- [ ] Enhance the existing audit log to capture all admin operations including view, edit, create, and delete actions across all modules
- [ ] Add an "Administrator" column/filter to see actions by specific admin users
- [ ] Add an "Operation type" filter (view, edit, create, delete, login, export) for targeted auditing
- [ ] Log "View" operations for sensitive data access (customer PII, financial data) to comply with data protection regulations
- [ ] Add Customer ID cross-reference filter to see all admin operations related to a specific subscriber

---

## 4.10 Internal System Log

**Screenshot reviewed:** `Internal.png`

**legacy BSS features observed:** An internal system operations log showing automated/batch processing events. Entries show timestamps at sub-second precision (e.g., "24/02/2026 00:00:12"), Operation types (Edit customer, Edit internet service), and Result status (Success). Filterable by Customer ID, Period, and Operation type. Shows high-frequency batch operations (multiple "Edit customer" entries per second during automated processing).

**Proposed improvements for DotMac Sub:**

- [ ] Add an internal/system operations log that captures automated batch processing events (Celery task operations, scheduled jobs, system-triggered status changes)
- [ ] Display sub-second timestamps for internal operations to support debugging of batch processing sequences
- [ ] Separate internal/automated operations from human-initiated operations in the audit log for clearer analysis
- [ ] Add batch operation grouping to collapse rapid-fire sequential operations into summarized entries

---

## 4.11 Portal Activity Log

**Screenshot reviewed:** `Portal.png`

**legacy BSS features observed:** A customer portal activity log tracking subscriber self-service actions. Columns: Date and time, Customer ID, Operation (Customer login, Customer logout), Result (Success). Filterable by Customer ID, Period date range, and Operation type. Shows login/logout patterns for customer self-service portal usage tracking.

**Proposed improvements for DotMac Sub:**

- [ ] Add a customer portal activity log at `/admin/system/audit/portal` tracking all subscriber portal login/logout and self-service actions
- [ ] Track login timestamps, session duration (derived from login-to-logout), and IP addresses for security monitoring
- [ ] Add failed login attempt tracking for subscriber accounts with alert thresholds
- [ ] Generate portal usage statistics (daily active users, peak login times, average session duration)
- [ ] Add geographic/IP-based anomaly detection for subscriber portal logins

---

## 4.12 Session Search

**Screenshot reviewed:** `Session.png`

**legacy BSS features observed:** A RADIUS/network session search tool with fields for Period (date range), IP address, IPv6 address, MAC address, Search scope (Customer statistics dropdown), and Fields selector (All selected). Allows searching active and historical network sessions by various identifiers.

**Proposed improvements for DotMac Sub:**

- [ ] Add a RADIUS session search tool at `/admin/network/sessions` allowing lookup by IP, IPv6, MAC address, and username
- [ ] Support date range filtering for historical session searches
- [ ] Add a "Search scope" selector to search across customer statistics, accounting records, or active sessions
- [ ] Display session results with connection duration, data usage, NAS device, and assigned IP information
- [ ] Add a "currently online" quick filter to see all active sessions in real time

---

## 4.13 File/Cron Logs

**Screenshot reviewed:** `Files.png`

**legacy BSS features observed:** A file-based log viewer showing system cron job and process logs. Entries include: api/error, billing/generate_preview, cron/accounting, cron/acme_sh, cron/backup-critical (263 KB), cron/backup-full (55 KB), cron/backup-radius-failover, cron/daily (and many daily sub-tasks like bonusCappedDataDailyProcess, cappedDataDailyProcess, changeStatusInvoicesWithZeroDue, checkAllLogs, checkNewVersionAddonsAvailability, closePublicAccessToTicket, expireNetFlowLogs, fixedCostsDailyProcess, fupResetLimits, generateInvoices, prepaidBillingDaily, prepaidCleanUp). Each shows Name, Description, Size, and an action to view contents.

**Proposed improvements for DotMac Sub:**

- [ ] Add a system log file viewer at `/admin/system/logs` showing Celery task execution logs, cron job outputs, and error logs
- [ ] Display log file sizes and last-modified timestamps for quick health assessment
- [ ] Add a log viewer with syntax highlighting, search, and tail-follow capability for real-time monitoring
- [ ] Organize logs by category (API errors, billing tasks, backup operations, daily maintenance tasks)
- [ ] Add log rotation and retention policy configuration from the admin UI
- [ ] Show backup operation logs (critical backup, full backup, RADIUS failover backup) with success/failure status

---

## 4.14 Email Logs

**Screenshot reviewed:** `Emails.png`

**legacy BSS features observed:** A comprehensive email delivery log showing all outbound emails. Columns include: ID, Recipient email, Type (Message), Status (Sent), Address source, Date/time sent, and Actions. Shows emails sent to various customer email addresses with delivery status tracking.

**Proposed improvements for DotMac Sub:**

- [ ] Add an email delivery log at `/admin/notifications/email-log` showing all outbound emails with recipient, subject, type, status, and timestamp
- [ ] Track email delivery status (queued, sent, delivered, bounced, failed) per message
- [ ] Add email resend capability from the log for failed deliveries
- [ ] Support filtering by recipient email, date range, status, and email type/template
- [ ] Link email log entries to the subscriber they relate to for cross-reference from subscriber detail pages

---

## 4.15 SMS Logs

**Screenshot reviewed:** `SMS.png`

**legacy BSS features observed:** An SMS delivery log table with columns: ID, Recipient phone number, Type (Message), Status (Error or Success indicators), Deliver date, Start time, and Details. Shows both successful and failed SMS deliveries with error tracking. Filterable by Period, destination, description, Type, and Status.

**Proposed improvements for DotMac Sub:**

- [ ] Add an SMS delivery log at `/admin/notifications/sms-log` tracking all outbound SMS messages
- [ ] Display SMS delivery status with clear success/error indicators and error detail messages
- [ ] Track SMS cost per message for provider billing reconciliation
- [ ] Add SMS retry capability for failed deliveries from the log view
- [ ] Add SMS delivery rate statistics (success rate, failure rate, average delivery time) as summary cards above the log table

---

## 4.16 Planned Customer Status and Service Changes

**Screenshot reviewed:** `Planned customer status and service changes.png`

**legacy BSS features observed:** A scheduled changes viewer split into two sections -- (1) Statuses: showing planned customer status transitions (e.g., customer 100000205 scheduled to become Inactive), and (2) Services: showing planned service changes with Date, Customer ID, Description (plan name), Price, Type (internet), and Plan link. Shows future-dated service activations and plan changes (e.g., "Unlimited Basic" at 17,500, "Homeflex Elite" at 25,500, "Unlimited Compact" at 35,000).

**Proposed improvements for DotMac Sub:**

- [ ] Add a "Scheduled Changes" dashboard at `/admin/provisioning/scheduled` showing all future-dated status transitions and service changes
- [ ] Support scheduling customer status changes (active to inactive, suspended to active) for a future date
- [ ] Support scheduling service/plan changes (upgrades, downgrades) for a future date with price preview
- [ ] Show separate sections for planned status changes and planned service changes
- [ ] Add calendar view option for visualizing scheduled changes across dates
- [ ] Send notification reminders before scheduled changes take effect

---

## 4.17 Customer Status and Service Changes Log

**Screenshot reviewed:** `customer status and service changes.png`

**legacy BSS features observed:** A historical log of all customer status transitions. Columns: Date and time (precise to seconds), Administrator (who made the change, including "[SYSTEM]" for automated changes), Customer ID, Status transition (e.g., "blocked -> active", "active -> blocked"), and Plan. Shows bulk automated blocking operations (system blocking many customers in rapid succession at 08:00-08:01) as well as manual admin changes (e.g., "r.okpe@dotmac.ng" changing "blocked -> active").

**Proposed improvements for DotMac Sub:**

- [ ] Add a subscriber status change history log showing all status transitions with who/what initiated them
- [ ] Distinguish between manual admin actions (show admin name/email) and automated system actions (show "[SYSTEM]")
- [ ] Add status transition arrow display format (e.g., "active -> blocked") for clear visual representation
- [ ] Track bulk automated operations (e.g., dunning/collections blocking) with timestamp patterns for operational analysis
- [ ] Add a "reverse" or "undo" action for recent manual status changes

---

## 4.18 Service Status Changes Log

**Screenshot reviewed:** `Service status changes.png`

**legacy BSS features observed:** A service-level status change log. Columns: Date and time, Administrator, Customer ID, Status transition (e.g., "Active -> Disabled", "Active -> Paused", "Paused -> Active", "-> Active"), Plan change (e.g., "Unlimited Basic -> Unlimited Compact"), and Service name link. Shows both status changes and plan migrations in a single view. Filterable by Customer ID, Period, and Admin.

**Proposed improvements for DotMac Sub:**

- [ ] Add a subscription/service status change log showing status transitions and plan migrations together
- [ ] Track both service status changes (Active -> Paused -> Disabled) and plan changes (Basic -> Compact) in the same log
- [ ] Show the service/plan name as a clickable link to the subscription detail page
- [ ] Filter service changes by administrator to track who is making operational changes
- [ ] Add plan change impact analysis showing price difference when a plan migration is logged

---

## 4.19 Accounting Integrations Log

**Screenshot reviewed:** `Accounting Integrations.png`

**legacy BSS features observed:** An accounting system sync log with four tabs: Customers, Invoices, Credit notes, and Payments. Shows sync status between legacy BSS and external accounting software. Columns: Customer name, Modified flag, Accounting ID (external system reference), Accounting Status (Ok, Error), Additional info fields (showing sync results like "Updated" or error messages such as "Object Not Found"), Created date, and Last updated timestamp. Filterable by Customer ID, Period, Modified status, and Accounting Status.

**Proposed improvements for DotMac Sub:**

- [ ] Add an accounting integration sync log at `/admin/integrations/accounting` with tabs for Customers, Invoices, Credit Notes, and Payments
- [ ] Track sync status per entity (Ok, Error, Pending) with external accounting system reference IDs
- [ ] Display detailed sync error messages for failed synchronizations
- [ ] Add "Modified" flag to identify records that have changed since last sync
- [ ] Support re-sync/retry for failed individual records from the log view
- [ ] Add bulk re-sync capability for all errored records

---

## 4.20 Mailjet Logs

**Screenshot reviewed:** `Mailjet Logs.png`

**legacy BSS features observed:** A Mailjet email service provider log viewer showing integration status. Currently displays "File not found" error, indicating the log file is not present or the integration is not active.

**Proposed improvements for DotMac Sub:**

- [ ] Add per-provider email delivery logs for each configured email service (Mailjet, SendGrid, SES, etc.)
- [ ] Show provider-specific delivery metrics (open rate, click rate, bounce rate) when the provider API supports it
- [ ] Display graceful empty/inactive state when a provider integration is not configured, rather than error messages

---

## 4.21 Paystack Logs

**Screenshot reviewed:** `Paystack Logs.png`

**legacy BSS features observed:** A Paystack payment gateway log showing transaction events. Columns: ID, Message (e.g., "Your transaction has been successfully processed", "Set payment method to bank_transfer", "Changed PIN", authorization errors like "Token Authentication Failed"), Status (color-coded badges: green for success, red for errors), Payment Number, Transaction Type (reference, status), Payment Type, Additional info (showing raw reference IDs and status codes), and Date/Time. Shows both successful and failed payment transactions with detailed error messages.

**Proposed improvements for DotMac Sub:**

- [ ] Add a payment gateway transaction log at `/admin/billing/payment-logs` showing all payment provider events
- [ ] Display payment transaction status with color-coded badges (success=green, error=red, pending=amber)
- [ ] Log detailed error messages for failed transactions (authentication failures, insufficient funds, expired tokens)
- [ ] Show payment reference IDs and provider-specific transaction identifiers for reconciliation
- [ ] Add payment event timeline per transaction (initiated -> processing -> success/failure)
- [ ] Support filtering by payment status, provider, date range, and customer

---

## 4.22 License Information

**Screenshot reviewed:** `License.png`

**legacy BSS features observed:** A system license information page showing product details: Product name (legacy BSS ISP Framework), Version (5.0.6584), Compiled date, Registered to (Dotmac Technologies Ltd), License limit (2000), Licensed customer count (1391 -- clickable). Includes action buttons: Reload license, Upgrade your license, Check validity.

**Proposed improvements for DotMac Sub:**

- [ ] Add a system information page at `/admin/system/about` showing application version, build date, and environment details
- [ ] Display current subscriber count against any configured limits for capacity planning
- [ ] Show database size, active connections, and system resource utilization
- [ ] Add version update check capability to notify admins of available updates
- [ ] Display deployment environment metadata (Python version, FastAPI version, PostgreSQL version, Redis version)

---

## 4.23 Documentation / Knowledge Base

**Screenshot reviewed:** `Documentation.png`

**legacy BSS features observed:** An integrated knowledge base with organized sections: Getting started, FAQs, Product updates. Main content sections organized by functional area: Customers, Leads, Tickets, Tariff plans, Networking, Customer portal. Left sidebar with expandable categories. Search functionality at the top. Covers CRM, Company, and System administration documentation.

**Proposed improvements for DotMac Sub:**

- [ ] Add an in-app help/documentation link from the admin sidebar that opens contextual help for the current page
- [x] Create a searchable knowledge base or help center integrated into the admin UI
- [ ] Add "Getting Started" guided setup wizard for new deployments
- [x] Include FAQ section accessible from the admin help menu
- [ ] Add contextual help tooltips on complex form fields referencing relevant documentation

---

## 4.24 Resellers Commission Report

**Screenshot reviewed:** `Resellers.png`

**legacy BSS features observed:** A resellers commission tracking report with three tabs: Resellers consolidated report, Reseller report, and Customer report. Columns: Reseller name, Commissioned transactions, Unpaid total, Total, and Commission. Period selector for date range filtering. Totals summary row at bottom. Mirrors the Agents report structure but scoped to reseller partners.

**Proposed improvements for DotMac Sub:**

- [ ] Add a reseller commission report at `/admin/reports/reseller-commissions` with period filtering
- [ ] Create three sub-views: consolidated (all resellers), per-reseller detail, and per-customer attribution
- [ ] Track commissioned transactions, unpaid totals, and earned commissions per reseller
- [ ] Support configurable commission structures per reseller (percentage, flat, tiered)
- [ ] Add commission payout tracking and export for accounting

---

## 4.25 Customers Chart

**Screenshot reviewed:** `Customers chart.png`

**legacy BSS features observed:** A time-series line chart showing customer status distribution over a 30-day period (24/01/2026 - 24/02/2026). Five data series: New (dark blue, near bottom), Active (light blue, ~1,500-2,300), Blocked (red, ~2,800 consistent), Inactive (grey, ~400 then dropping), and Total (purple, ~4,900-5,300). Shows a notable inflection point around 05-06/02/2026 where Active customers jumped from ~1,500 to ~2,300 and Total increased from ~4,900 to ~5,300. Date-range picker for customization.

**Proposed improvements for DotMac Sub:**

- [ ] Add a subscriber growth/status chart to the admin dashboard showing daily subscriber counts by status over time
- [ ] Display time-series lines for: New, Active, Suspended/Blocked, Inactive/Canceled, and Total subscribers
- [ ] Add date range picker for customizable reporting periods (7d, 30d, 90d, 1y, custom)
- [ ] Highlight inflection points and anomalies (sudden drops or spikes) with annotations
- [ ] Calculate and display growth rate metrics (daily, weekly, monthly net subscriber change)
- [ ] Add chart export capability (PNG, PDF) for management reporting

---

## 4.26 Internet Plan Usage Report

**Screenshot reviewed:** `Internet usage report.png`

**legacy BSS features observed:** An internet plan usage report aggregating bandwidth consumption by plan. Columns: Plan name, Count of services, Partner, Download (GB), Upload (GB), Total Down/Up (GB). Filterable by Period, Partners, Locations, and Type (By counters). Provides aggregate usage data per tariff plan for capacity planning.

**Proposed improvements for DotMac Sub:**

- [ ] Add a bandwidth usage report per plan at `/admin/reports/usage-by-plan` showing aggregate download/upload totals per tariff plan
- [ ] Display service count per plan alongside usage data for per-subscriber average calculation
- [ ] Filter by partner/reseller and location for scoped usage analysis
- [ ] Support "By counters" and "By sessions" calculation types for different RADIUS accounting methods
- [ ] Add usage trend visualization (chart) showing per-plan consumption growth over time

---

## 4.27 Customer Internet Usage Report

**Screenshot reviewed:** `Customer internet usage.png`

**legacy BSS features observed:** A per-customer internet usage report with detailed columns: Status, Customer ID, Portal login, Full name, Phone number, Internet plans, IPs assigned, VAT ID, Building Type (custom field), Download total, Upload total. Filterable by Period, Locations, and Partners. Provides granular per-subscriber bandwidth consumption data.

**Proposed improvements for DotMac Sub:**

- [ ] Add a per-subscriber bandwidth usage report at `/admin/reports/customer-usage` showing individual data consumption
- [ ] Include subscriber details (name, plan, IP, phone) alongside download/upload totals for context
- [ ] Support custom field columns (building type, service area) in the usage report
- [ ] Add data export (CSV/Excel) for the per-customer usage report
- [ ] Flag heavy users (top percentile consumers) for network capacity planning

---

## 4.28 Finance Logs

**Screenshot reviewed:** `Finance logs.png`

**legacy BSS features observed:** A financial operations log with three tabs: Future charges (showing upcoming scheduled charges with Customer login, Customer name, Transactions count, Will be charged amount, Account balance), Daily receipt, and Charge history. Filterable by Period, Partner, and Location. Includes a "Generate" action button (presumably to process pending charges). Shows transaction previews before they are executed.

**Proposed improvements for DotMac Sub:**

- [ ] Add a "Future charges" preview at `/admin/billing/upcoming-charges` showing pending scheduled billing events before execution
- [ ] Add a "Daily receipt" report showing all financial transactions processed on a given day
- [ ] Add a "Charge history" log showing historical billing execution results
- [ ] Include account balance alongside charge amount to identify subscribers who will go negative
- [ ] Add a "Generate/Process" action to manually trigger pending charge processing
- [ ] Filter by partner and location for scoped financial operations

---

## 4.29 Financial Report Per Plan

**Screenshot reviewed:** `Financial report per plan.png`

**legacy BSS features observed:** A comprehensive revenue report by tariff plan with two components: (1) A bar chart showing "Top 20 compared with another period" with three color-coded series (Main period in green, Period for comparison in yellow, Difference in blue) displaying revenue per plan (Unlimited Basic, Unlimited Compact, 50 Mbps Fiber, etc. -- values up to 20,000,000 Naira), and (2) A detailed table below with columns: Plan, Plan price, Transactions count, Sum of transactions for selected period, Invoiced amount, Discount, Charge total, and Charge total for compared period. Filterable by Main period, Plans, Partner, and Location. Values in Nigerian Naira.

**Proposed improvements for DotMac Sub:**

- [ ] Add a revenue-per-plan report at `/admin/reports/revenue-per-plan` with period-over-period comparison
- [ ] Include a bar chart visualization showing top plans by revenue with main period vs. comparison period
- [ ] Display plan price, transaction count, invoiced amount, discounts applied, and net charge totals in a detail table
- [ ] Support period-over-period comparison (e.g., this month vs. last month) with difference calculation
- [ ] Filter by plan, partner/reseller, and location for segmented analysis
- [ ] Show currency-formatted values appropriate to the organization's locale

---

## 4.30 Invoice Report

**Screenshot reviewed:** `Invoice report.png`

**legacy BSS features observed:** A detailed invoice listing with columns: Invoice number (formatted as date-based sequence), Status (color-coded "Paid" badges in green), Customer ID, Customer name, VAT amount, Net amount, Total amount (showing values like 7,500/17,500/21,500 Naira), Building Type (custom field). Shows all invoices with payment status for financial reconciliation.

**Proposed improvements for DotMac Sub:**

- [ ] Enhance the existing invoices report to include VAT/tax breakdown columns (VAT amount, net amount, total)
- [ ] Add color-coded payment status badges (Paid=green, Unpaid=rose, Partial=amber, Overdue=red)
- [ ] Support custom field columns in the invoice report (building type, service area, etc.)
- [ ] Add invoice number formatting with configurable prefix/pattern (e.g., date-based sequences)
- [ ] Add bulk invoice export (PDF batch, CSV summary) from the report view

---

## 4.31 Statements Report

**Screenshot reviewed:** `Statements.png`

**legacy BSS features observed:** A customer statements module with three tabs: Statements (showing Customer login, Customer name, Finance documents, Opening balance, Closing balance), Finance customers report, and Receivables aging report. Period selector and Type filter (Transactions). Action buttons: Show, Generate PDF, Send to customers, and Filter. Supports bulk statement generation and distribution.

**Proposed improvements for DotMac Sub:**

- [ ] Add a customer statements report at `/admin/billing/statements` showing per-customer financial summaries with opening and closing balances
- [ ] Add a "Finance customers report" sub-tab summarizing financial status per customer
- [ ] Add a "Receivables aging report" sub-tab showing outstanding balances bucketed by age (30/60/90/120+ days)
- [ ] Support bulk PDF statement generation for all customers or a filtered subset
- [ ] Add "Send to customers" bulk action to email statements to all subscribers
- [ ] Include transaction type filtering (invoices only, payments only, all transactions)

---

## 4.32 Tax Reports

**Screenshot reviewed:** `Tax Reports.png`

**legacy BSS features observed:** A tax reporting module with two tabs: Tax report and Tax totals report. Table columns: Document number, Status (color-coded "Paid" badges), Customer ID, Customer name, Transaction count, Description (e.g., "Radio Installation"), VAT rate, Net amount, VAT amount (with values in Naira), Building Type. Shows per-transaction tax detail for regulatory compliance.

**Proposed improvements for DotMac Sub:**

- [ ] Add a tax report at `/admin/reports/tax` showing per-invoice tax details with VAT rate, net amount, and tax amount
- [ ] Add a "Tax totals" summary sub-tab aggregating total tax collected by rate/category
- [ ] Include document number, customer reference, and transaction description for audit-ready reporting
- [ ] Support multi-rate tax reporting (different VAT rates for different service types)
- [ ] Add tax report export in formats required by local tax authorities

---

## 4.33 Custom Prices and Discounts Report

**Screenshot reviewed:** `Custom prices and discounts.png`

**legacy BSS features observed:** A custom pricing and discount report showing all non-standard pricing across the customer base. Columns: Type, Customer ID, Customer name, Customer login, Partner, Location, ID, Description, Plan, Tariff price, Unit price, Unit, Billing start/end date, Period, Discount value, Discount type, Discount start/end date. Filterable by Partner and Location. Provides visibility into all pricing overrides and active discounts.

**Proposed improvements for DotMac Sub:**

- [ ] Add a custom prices and discounts report at `/admin/reports/custom-pricing` listing all non-standard pricing overrides and active discounts
- [ ] Show the difference between standard tariff price and custom unit price for each subscriber
- [ ] Display discount type (percentage, fixed amount), value, and validity period
- [ ] Filter by partner, location, plan, and discount type
- [ ] Add alerts for expiring discounts (discounts ending within 30 days)
- [ ] Support bulk discount expiration/renewal from the report view

---

## 4.34 Transactions Categories Report

**Screenshot reviewed:** `Transactions categories.png`

**legacy BSS features observed:** A revenue breakdown by transaction/service category. Columns: ID, Revenue stream (category name), Invoices count, Transactions count, Income, Tax, Income with Tax, Data top-ups, Data top-ups with Tax, Average service price, Average top-up. Filterable by Period and Partner. Provides ARPU (Average Revenue Per User) and revenue composition analysis.

**Proposed improvements for DotMac Sub:**

- [ ] Add a revenue by category report at `/admin/reports/revenue-categories` showing income breakdown by service/transaction type
- [ ] Calculate and display ARPU (Average Revenue Per User/Service) per category
- [ ] Show invoice count, transaction count, gross income, tax, and net income per category
- [ ] Track data top-up revenue separately from subscription revenue
- [ ] Support period filtering and partner scoping for multi-tenant revenue analysis

---

## 4.35 Customer Contracts Report

**Screenshot reviewed:** `Customer Contracts.png`

**legacy BSS features observed:** A contract management report with three tabs: Contracts pending signature, Expiring contracts, and Signed contracts. Table columns: ID, Customer, Title, Description, and Date. Provides lifecycle tracking of customer service contracts from creation through signature to expiration.

**Proposed improvements for DotMac Sub:**

- [ ] Add a customer contracts management module at `/admin/legal/contracts` with lifecycle tracking
- [ ] Create three contract views: Pending Signature, Expiring Soon (within configurable window), and Signed/Active
- [ ] Support digital contract signing workflow (generate -> send -> track -> archive)
- [ ] Add contract expiration alerting for contracts expiring within 30/60/90 days
- [ ] Link contracts to subscriber records and display contract status on subscriber detail page
- [ ] Support contract template management for standardized service agreements

---

## 4.36 Monthly Recurring Revenue (MRR) Net Change Report

**Screenshot reviewed:** `Monthly recurring revenue net change report.png`

**legacy BSS features observed:** An MRR net change report with columns: Month start (subscriber count), New services (additions), Cancellations, Month end (subscriber count), and Net change. Filterable by Year, Partners, and Locations. Tracks monthly subscriber base movement for growth analysis.

**Proposed improvements for DotMac Sub:**

- [ ] Add an MRR (Monthly Recurring Revenue) net change report at `/admin/reports/mrr` showing month-over-month subscriber and revenue movement
- [ ] Track new service activations, cancellations/churn, and net change per month
- [ ] Display month-start and month-end subscriber counts for cohort tracking
- [ ] Filter by year, partner, and location for segmented analysis
- [ ] Add MRR trend chart visualization with trendline projection
- [ ] Calculate MRR expansion (upgrades) and contraction (downgrades) separately from new/churn

---

## 4.37 New Services Report

**Screenshot reviewed:** `New services report.png`

**legacy BSS features observed:** A detailed new service activations report. Columns: Type (icon), Customer name, Region, Description, Plan name, Unit price (e.g., 17,500 / 10,000 Naira), Billing start date, Status (color-coded: Active in green, Blocked in red, Disabled), Connections (showing service identifiers), and Address. Filterable by Period, Partners, Locations, and Status. Shows all new service installations with pricing and location detail.

**Proposed improvements for DotMac Sub:**

- [ ] Add a new services/activations report at `/admin/reports/new-services` listing all newly provisioned services with activation details
- [ ] Include customer name, plan, price, activation date, status, and physical address
- [ ] Add status filtering (Active, Blocked, Disabled) to see activation success rates
- [ ] Show connection identifiers (PPPoE username, IP address) for technical cross-reference
- [ ] Calculate activation metrics: average time-to-activate, activation success rate, revenue from new services

---

## 4.38 Service Export

**Screenshot reviewed:** `Service export.png`

**legacy BSS features observed:** A configurable service data export tool. Filter options: Types (service types), Status (Any), Partners, Attributes (selectable data fields), Delimiter (Comma/Tab/Semicolon). Toggle options: "First row contains column names" and "Ignore services from bundle". Single "Export" action button generates a downloadable CSV/file.

**Proposed improvements for DotMac Sub:**

- [ ] Add a configurable data export tool at `/admin/system/export` supporting multiple entity types (subscribers, services, invoices, payments)
- [ ] Allow column/attribute selection for custom export schemas
- [ ] Support multiple delimiters (comma, tab, semicolon) and file formats (CSV, Excel, JSON)
- [ ] Add "First row contains column names" toggle for header inclusion
- [ ] Support status and partner filtering before export
- [ ] Save export configurations as templates for recurring exports
- [ ] Add scheduled/automated export capability (e.g., weekly subscriber export to SFTP)

---

## 4.39 Ticket Reports

**Screenshot reviewed:** `Ticket reports.png`

**legacy BSS features observed:** A comprehensive ticket/support reporting module with seven tabs: Closed tickets, SLA report, Agent Performance, Performance distribution report, Ticket lifecycle, Activity per admin, and Cost of support. Table columns: ID, Created date, Resolved date, Assigned to, Subject, Comment, Customer feedback, Content, Timing, Feedback, and Grade. Filterable by Period, Show (All/Specific), Date of (Resolve/Create), and Assign to. Provides deep support operations analytics.

**Proposed improvements for DotMac Sub:**

- [ ] Add a support ticket reporting module at `/admin/reports/tickets` (if/when ticketing is added to DotMac Sub)
- [ ] Include SLA compliance reporting (response time vs. target, resolution time vs. target)
- [ ] Add agent performance metrics (tickets resolved, average resolution time, customer satisfaction score)
- [ ] Track ticket lifecycle stages with timing metrics per stage
- [ ] Add "Cost of support" analysis calculating support cost per subscriber
- [ ] Include customer feedback/satisfaction scoring for closed tickets

---

## 4.40 Referral System Report

**Screenshot reviewed:** `Referral system Report.png`

**legacy BSS features observed:** A customer referral program tracking report. Columns: ID, Referrer (referring customer), Referrer bonus, Referee (referred customer), Referee bonus, Credit note date, Invitation send date, Account creation date, Activation date, and Tariffs (plan subscribed). Period selector for date range filtering.

**Proposed improvements for DotMac Sub:**

- [ ] Add a customer referral program module with referrer/referee tracking
- [ ] Create a referral report at `/admin/reports/referrals` showing referral chains with bonus amounts
- [ ] Track referral lifecycle: invitation sent -> account created -> service activated -> bonus credited
- [ ] Support configurable referral bonuses (credit note, discount, cash) for both referrer and referee
- [ ] Auto-generate credit notes when referred customers activate service
- [ ] Add referral link/code generation for subscribers to share via portal

---

## 4.41 Refill Cards Statistics

**Screenshot reviewed:** `Refill cards statistics.png`

**legacy BSS features observed:** A prepaid refill/voucher card statistics report. Columns: Serie (card series/batch), Partner, Price, Amount (face value), Total used, Total active, Total expired, Total disabled, Total amount used, Total amount active, Total amount expired, Total amount disabled. Filterable by Period and Partner. Tracks voucher inventory and redemption across card batches.

**Proposed improvements for DotMac Sub:**

- [ ] Add a prepaid voucher/refill card management module for ISPs that offer prepaid internet plans
- [ ] Track voucher batches (series) with inventory counts: active, used, expired, disabled
- [ ] Display monetary totals per batch (amount used, amount active, amount expired)
- [ ] Support voucher generation in bulk batches with configurable denominations
- [ ] Add voucher redemption tracking linked to subscriber accounts
- [ ] Filter voucher statistics by partner for reseller-scoped prepaid operations

---

## 4.42 DNS Threats Archive

**Screenshot reviewed:** `DNS threat archive.png`

**legacy BSS features observed:** A DNS security/threat intelligence integration (Whalebone) showing an error state: "whalebone_api_region is not set. Please check your addon config!" This is a DNS-based threat detection and blocking system that archives DNS threat events for ISP network security monitoring.

**Proposed improvements for DotMac Sub:**

- [ ] Add DNS-based threat intelligence integration support for ISP network security
- [ ] Create a DNS threats dashboard showing blocked domains, threat categories, and affected subscribers
- [ ] Support integration with DNS threat intelligence providers (Whalebone, Cisco Umbrella, etc.)
- [ ] Archive DNS threat events with subscriber attribution for security incident investigation
- [ ] Add threat statistics reporting (threats blocked per day, top threat categories, most-targeted subscribers)

---

## Priority Summary

### P0 -- Critical (Core administration gaps that limit daily operations)

| # | Improvement | Section |
|---|------------|---------|
| 1 | Unified Administration hub page with categorized links and search | 4.1 |
| 2 | Subscriber status change history log with admin attribution | 4.17 |
| 3 | Service/subscription status change log with plan migration tracking | 4.18 |
| 4 | Email delivery log with status tracking and resend capability | 4.14 |
| 5 | Payment gateway transaction log with error details (Paystack, Flutterwave) | 4.21 |
| 6 | Receivables aging report (30/60/90/120+ day buckets) | 4.31 |
| 7 | Enhanced API key management with named keys, scoping, and usage tracking | 4.7 |

### P1 -- High (Reporting and analytics that drive business decisions)

| # | Improvement | Section |
|---|------------|---------|
| 8 | Subscriber growth/status chart (time-series visualization on dashboard) | 4.25 |
| 9 | Revenue-per-plan report with period-over-period comparison and chart | 4.29 |
| 10 | MRR net change report (new services, cancellations, net growth per month) | 4.36 |
| 11 | Financial report per plan with comparison period | 4.29 |
| 12 | New services/activations report with status and pricing | 4.37 |
| 13 | Tax report with per-invoice VAT detail and totals summary | 4.32 |
| 14 | Customer statements with bulk PDF generation and email distribution | 4.31 |
| 15 | Finance logs with future charges preview and charge history | 4.28 |

### P2 -- Medium (Operational efficiency and partner/reseller features)

| # | Improvement | Section |
|---|------------|---------|
| 16 | ISP-specific predefined role templates (NOC, engineer, frontdesk, etc.) | 4.3 |
| 17 | Admin user impersonation capability for super-admins | 4.2 |
| 18 | Partners/resellers page with real-time online customer counts | 4.4 |
| 19 | Reseller commission tracking and reporting | 4.24 |
| 20 | Sales agent commission module with per-agent attribution | 4.5 |
| 21 | Locations management with customer counts and tax association | 4.6 |
| 22 | Planned/scheduled status and service changes scheduler | 4.16 |
| 23 | Configurable data export tool with column selection and format options | 4.38 |
| 24 | Operations audit log with admin and operation type filtering | 4.9 |
| 25 | API request audit log with key, operation, and result tracking | 4.8 |

### P3 -- Low (Advanced features and integrations for future roadmap)

| # | Improvement | Section |
|---|------------|---------|
| 26 | SMS delivery log with success/error tracking | 4.15 |
| 27 | Customer portal activity log (login/logout/session tracking) | 4.11 |
| 28 | RADIUS session search by IP, MAC, username | 4.12 |
| 29 | System log file viewer for Celery tasks and cron jobs | 4.13 |
| 30 | Internal/automated operations log separated from manual actions | 4.10 |
| 31 | Accounting integration sync log with error tracking | 4.19 |
| 32 | Per-subscriber bandwidth usage report | 4.27 |
| 33 | Internet plan usage report (aggregate bandwidth per plan) | 4.26 |
| 34 | Custom prices and discounts report | 4.33 |
| 35 | Transaction categories revenue breakdown with ARPU | 4.34 |
| 36 | Customer contracts lifecycle management | 4.35 |
| 37 | Customer referral program with bonus tracking | 4.40 |
| 38 | Prepaid voucher/refill card management | 4.41 |
| 39 | DNS threat intelligence integration | 4.42 |
| 40 | Support ticket reporting (SLA, agent performance, cost of support) | 4.39 |
| 41 | In-app knowledge base / contextual help system | 4.23 |
| 42 | System version/about page with environment details | 4.22 |
| 43 | Provider-specific email delivery logs (Mailjet, SendGrid) | 4.20 |

---

*Document generated from review of 42 legacy BSS Administration screenshots. All features described are observations from legacy BSS ISP Framework v5.0 as deployed at Dotmac Technologies Ltd. Proposed improvements are adapted for DotMac Sub's FastAPI/HTMX/Tailwind architecture.*
