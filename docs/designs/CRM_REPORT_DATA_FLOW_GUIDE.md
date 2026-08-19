# Self-Care CRM report data-flow guide

Status: implementation handover for PR #2330

Owner: `ui.crm_operational_reports` for report projections. The services named
below remain authoritative for their underlying facts.

All report pages are read-only. Unless a report explicitly names an external
gateway, it reads committed Self-Care records at request time. The module named
`crm_api` is an internal Self-Care service, not a call to the legacy CRM.

## NCC complaints

REPORT: NCC Complaints
Data source: Native support Tickets, TicketComments, Subscribers, addresses, assignees, and service teams.
Backend query/service: typed `ncc_complaints_report.query_report`; compatibility adapters use `build_report`; scheduled delivery is owned by `communications.ncc_weekly_delivery`.
Transformation/calculation: Filters the requested Ticket-created window, excludes cancelled/merged/test records and tickets with approved internal operational provenance, projects supported Nigerian phones into NCC's `234XXXXXXXXXX` format, maps stored category/channel/status into NCC vocabulary, derives SLA status only when authoritative timestamps exist, canonicalizes geography, and validates filing readiness. Incomplete customer complaints remain visible as validation failures.
Route/API: `/admin/reports/ncc-complaints`; on-demand XLSX at `/admin/reports/ncc-complaints/export`; preserved scheduled XLSX at `/admin/reports/ncc-weekly-runs/{run_id}/download`.
UI component/template: `templates/admin/reports/ncc_complaints.html`.
Displayed as: Complaints, Not Yet Filable, and Unclassified cards, filing-readiness table, on-demand workbook, complete Tuesday delivery configuration, and recent run/delivery/artifact evidence.
Permission: `provisioning:read`; notification-setting writes separately require `notification:write`.
Ownership: Self-Care-owned.
Data freshness/synchronization: Live request-time database read. The scheduler polls every five minutes; the owner admits one configured Tuesday occurrence after local delivery time and retries recorded failures.
Plain-English flow: Self-Care support records are translated into the NCC filing contract without inventing missing classifications. On Tuesday, the delivery owner preserves the exact validated workbook and queues it once to the configured To/CC/BCC recipients.

## NCC regulatory pack

REPORT: NCC Regulatory Pack
Data source: Native complaints and subscriber/capacity facts plus the explicit back-office financial and workforce gateway.
Backend query/service: `ncc_regulatory_pack.build_regulatory_pack`, `ncc_complaints_report`, `ncc_subscriber_report`, and the configured back-office client.
Transformation/calculation: Builds complaints, subscriber/capacity, financial, and workforce sections independently; subscriber facts are grouped by connection, customer, billing, speed, state, and geopolitical region. Missing external sections remain unavailable.
Route/API: `/admin/reports/ncc-pack`; JSON `/admin/reports/ncc-regulatory-pack`; PDF `/admin/reports/ncc-regulatory-pack.pdf`; subscriber detail `/admin/reports/ncc-subscribers`.
UI component/template: `ncc_pack.html` and `ncc_subscribers.html`.
Displayed as: COMPLETE/INCOMPLETE pack state, per-section availability/errors, JSON/PDF links, and the subscriber detail view.
Permission: `provisioning:read`; subscriber detail uses `customer:read`.
Ownership: Mixed Self-Care-owned and external; the assembled pack is a Self-Care projection.
Data freshness/synchronization: Native facts and external gateway results are read during pack construction; failures have no fabricated fallback.
Plain-English flow: Self-Care combines its regulatory records with explicitly owned back-office facts and reports each source's availability.

## Network infrastructure

REPORT: Network Infrastructure
Data source: OLTDevice, OntUnit, IpPool, IpBlock, Vlan, PonPort, FiberStrand, FdhCabinet, and Splitter records.
Backend query/service: `crm_reporting.network_infrastructure_facts`, composed by `web_reports.get_network_report_data`; IP utilization comes from `ip_pool_utilization_snapshot.live_pool_counts`.
Transformation/calculation: Counts active/total OLTs, observed-online ONTs, IP usage, VLANs, PON capacity/utilization, available fibre, FDHs, and splitter outputs; selects ten recent ONT observations.
Route/API: `/admin/reports/network`; CSV `/admin/reports/network/export`.
UI component/template: `templates/admin/reports/network.html`.
Displayed as: Infrastructure/capacity cards, device-health and IP-pool charts, OLT/pool lists, and recent ONT observations.
Permission: `reports:network:read`; CSV requires `reports:network:export`.
Ownership: Network observations are projected/synchronized into Self-Care; the persisted projection is the report source.
Data freshness/synchronization: Live report query over the latest collector/synchronizer results.
Plain-English flow: Network collectors update native inventory and runtime observations, the typed report owner aggregates them, and the web adapter formats the result for HTML or CSV.

## Subscriber overview

REPORT: Subscriber Overview
Data source: Subscriber, Subscription, CatalogOffer, BandwidthSample, and Ticket records.
Backend query/service: `web_reports.get_subscribers_report_data`, `crm_reporting.subscriber_segment_facts`, `subscriber_growth`, and `usage_summary.period_usage_by_subscriber`.
Transformation/calculation: Applies visibility/date/status filters and pagination; groups plan, region, status, growth, and ticket cohorts; converts sampled average bits/second across the period into estimated bytes, GB, and Mbps per subscriber.
Route/API: `/admin/reports/customers` and `/admin/reports/subscribers`; matching CSV aliases.
UI component/template: `templates/admin/reports/subscribers.html`.
Displayed as: Customer KPI cards, growth/status/plan/region views, paginated customer rows, usage GB, average Mbps, and recent sign-ups.
Permission: `customer:read`.
Ownership: Customer state is Self-Care-owned; bandwidth observations are projected into Self-Care.
Data freshness/synchronization: Live query; usage freshness follows bandwidth sampling.
Plain-English flow: The visible customer cohort is enriched with native plan, support, and sampled-usage facts before the report is paginated and rendered.

## Churned subscribers

REPORT: Churned Subscribers
Data source: Subscriber lifecycle state and Subscription cancellation reasons.
Backend query/service: `web_reports.get_churn_report_data`, `subscriber_growth`, and `crm_reporting.subscription_churn_reason_counts`.
Transformation/calculation: Calculates strict cancelled, active, and suspended cohorts, churn/retention percentages, monthly churn, and stored cancellation-reason counts with an explicit missing-reason bucket.
Route/API: `/admin/reports/churn`; CSV `/admin/reports/churn/export`.
UI component/template: `templates/admin/reports/churn.html`.
Displayed as: Churn, cancellation, at-risk, and retention cards; trend/reason views; recent cancellations.
Permission: `customer:read`.
Ownership: Self-Care-owned; CRM retention engagement records are excluded.
Data freshness/synchronization: Live native lifecycle read.
Plain-English flow: Native subscriber/service cancellation state is summarized directly without treating CRM outreach or dispositions as lifecycle truth.

## Technician performance

REPORT: Technician Performance
Data source: InstallAppointment, ProvisioningTask, and ServiceOrder.
Backend query/service: `provisioning_managers.technician_report_stats` and `recent_completed_appointments`, composed by `web_reports.get_technician_report_data`.
Transformation/calculation: Applies one inclusive date window to appointment schedule, task completion, and order creation facts; calculates completed appointments, task duration, completion rate, technician groups, and order types.
Route/API: `/admin/reports/technician`; CSV `/admin/reports/technician/export`.
UI component/template: `templates/admin/reports/technician.html`.
Displayed as: Technician/jobs/duration/completion cards, leaderboard, job types, and recent completions.
Permission: `reports:support:read`.
Ownership: Self-Care-owned provisioning data.
Data freshness/synchronization: Live period query.
Plain-English flow: The provisioning owner calculates field-work statistics and the web adapter presents the same period semantics in the page and export.

## Online activity

REPORT: Online Activity
Data source: Subscriber, active Subscription, and RadiusAccountingSession.
Backend query/service: Internal `crm_api.online_subscribers`, mapped by `crm_reporting`.
Transformation/calculation: Uses the newest non-stopped, non-ended session per subscriber where coalesced last-update/session-start/creation time is within 24 hours.
Route/API: `/admin/reports/operational/online-activity`; CSV under `/export`.
UI component/template: `templates/admin/reports/operational.html`.
Displayed as: Online Customers card and subscriber-number/status/last-activity table.
Permission: `customer:read`.
Ownership: RADIUS observations are external facts projected into Self-Care.
Data freshness/synchronization: Live query with a 24-hour freshness threshold; effective freshness follows interim accounting updates.
Plain-English flow: Recent RADIUS session evidence is reduced to one live observation per subscriber and displayed through the shared report UI.

## Subscriber billing risk

REPORT: Subscriber Billing Risk
Data source: Subscriber, Subscription, Invoice, Payment, EnforcementLock, DunningCase, and CatalogOffer.
Backend query/service: Internal `crm_api.billing_risk_rows`, mapped by `crm_reporting`.
Transformation/calculation: Uses canonical open-invoice filters, successful payments, next-billing dates, and enforcement/dunning dates; retains accounts with debt or a block and sorts by balance.
Route/API: `/admin/reports/operational/billing-risk`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: At-risk, Outstanding, and Blocked cards plus customer billing-risk rows.
Permission: `reports:billing:read`.
Ownership: Self-Care-owned billing/account state.
Data freshness/synchronization: Live current-position query; no date filter.
Plain-English flow: Current debt, payment timing, billing cadence, and enforcement evidence are combined without importing CRM engagement state.

## Subscriber revenue and pipeline

REPORT: Subscriber Revenue & Pipeline
Data source: Subscriber, Invoice, Payment, current MRR, and ServiceOrder.
Backend query/service: `crm_reporting` financial aggregation and revenue builder.
Transformation/calculation: Sums period invoiced, collected, and outstanding amounts per subscriber and adds current MRR plus non-terminal service-order counts.
Route/API: `/admin/reports/operational/subscriber-revenue`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: Invoiced, Collected, Outstanding, and Open Service Orders cards plus subscriber financial rows.
Permission: `reports:billing:read`.
Ownership: Self-Care-owned billing and provisioning facts.
Data freshness/synchronization: Live; date filters affect invoices/payments while MRR and open orders remain current snapshots.
Plain-English flow: Realized billing activity is placed beside current recurring revenue and pending service delivery.

## Postpaid customers

REPORT: Postpaid Customers
Data source: Postpaid Subscriber records, Invoice, and Payment.
Backend query/service: `crm_reporting` postpaid builder and financial aggregation.
Transformation/calculation: Selects postpaid accounts and totals invoiced, collected, and outstanding amounts.
Route/API: `/admin/reports/operational/postpaid-customers`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: Postpaid Customers and Outstanding cards plus billing-day and balance rows.
Permission: `reports:billing:read`.
Ownership: Self-Care-owned.
Data freshness/synchronization: Live all-time/current-position query.
Plain-English flow: The postpaid cohort is joined to its authoritative billing position and displayed without a CRM copy.

## CRM performance

REPORT: CRM Performance
Data source: ServiceTeam, InboxConversationTeam, InboxConversation, InboxMessage, and InboxConversationAssignment.
Backend query/service: `team_inbox_metrics.team_performance_report`, mapped by `crm_reporting`.
Transformation/calculation: Counts workload/open/assignment/response facts per team and measures first response and queue wait from authoritative timestamps.
Route/API: `/admin/reports/operational/crm-performance`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: Teams, Conversations, and Open cards plus team performance rows.
Permission: `reports:support:read`.
Ownership: Self-Care-owned team-inbox projection.
Data freshness/synchronization: Live current-history query.
Plain-English flow: Inbox conversations, messages, and assignments become team workload and responsiveness measures; retention/campaign state is not involved.

## Administrative agent performance

REPORT: Administrative Agent Performance
Data source: ServiceTeamMember, ServiceTeam, SystemUser, assignments, conversations, and authored inbox messages.
Backend query/service: `team_inbox_metrics.agent_performance_report`, mapped by `crm_reporting`.
Transformation/calculation: Counts active assignments and distinct handled conversations and calculates assignment wait and first human response per active team member.
Route/API: `/admin/reports/operational/agent-performance`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: Agents and Handled cards plus searchable, paginated per-agent/team timing rows.
Permission: `reports:support:read`.
Ownership: Self-Care-owned team-inbox projection.
Data freshness/synchronization: Live current-history query.
Plain-English flow: Recorded assignment and message-author evidence is grouped by active team member for an administrative view.

## Personal agent performance

REPORT: Personal Agent Performance
Data source: The same team-inbox records as administrative agent performance.
Backend query/service: `team_inbox_metrics.agent_performance_report`, fail-closed filtered by the signed-in principal in `crm_reporting`.
Transformation/calculation: Retains only rows whose person identifier matches the authenticated user; missing or invalid identity returns no rows.
Route/API: `/admin/reports/operational/my-performance`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: The signed-in agent's handling cards and performance rows.
Permission: `reports:support:read` plus mandatory personal identity scope.
Ownership: Self-Care-owned.
Data freshness/synchronization: Live current-history query.
Plain-English flow: The common agent calculation is narrowed to the authenticated person before it reaches the UI or export.

## Operations SLA violations

REPORT: Operations SLA Violations
Data source: SlaBreach, SlaClock, Ticket, Project, and ProjectTask.
Backend query/service: `ticket_sla_reports.violation_records` plus `crm_reporting`.
Transformation/calculation: Combines confirmed active Ticket SLA breaches with projects/tasks whose completion or current time exceeds their due date and normalizes lateness to hours.
Route/API: `/admin/reports/operational/operations-sla`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: Violations card and work-type/reference/status/due/completion/overdue table.
Permission: `reports:support:read`.
Ownership: Self-Care-owned SLA and project/task state.
Data freshness/synchronization: Live; Ticket filtering uses breach time and project/task filtering uses due date.
Plain-English flow: Support breaches and overdue delivery work are normalized into one operational exception list.

## Queue wait and issue classification

REPORT: Queue Wait & Issue Classification
Data source: InboxConversationQueueEntry and InboxConversation metadata/tags.
Backend query/service: `crm_reporting`.
Transformation/calculation: Uses recorded AI department/classification or marks Unclassified, joins tags, and calculates settled wait from queue entry to settlement; unsettled entries have no invented duration.
Route/API: `/admin/reports/operational/queue-classification`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: Queue Entries, Average Settled Wait, and Unclassified cards plus classification rows.
Permission: `reports:support:read`.
Ownership: Classification observations are projected into Self-Care inbox metadata.
Data freshness/synchronization: Live period query.
Plain-English flow: Queue events are paired with the conversation's recorded issue evidence and settled events provide the wait-time measurement.

## Subscriber lifecycle

REPORT: Subscriber Lifecycle
Data source: Subscriber and Subscription.
Backend query/service: `crm_reporting`.
Transformation/calculation: Counts subscriber/service states for records created in the period and groups cancellations by stored reason.
Route/API: `/admin/reports/operational/subscriber-lifecycle`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: Subscribers, Services, and Cancelled Services cards plus lifecycle-state/reason counts.
Permission: `customer:read`.
Ownership: Self-Care-owned; raw CRM retention records are excluded.
Data freshness/synchronization: Live period query.
Plain-English flow: Native account and service lifecycle records are counted directly, without creating a second retention state machine.

## Subscriber service quality

REPORT: Subscriber Service Quality
Data source: Subscriber, Ticket, WorkOrder, Subscription, and CustomerOutageInterval.
Backend query/service: `crm_reporting`.
Transformation/calculation: Counts tickets/work orders per subscriber, maps outage intervals through subscriptions, and sums closed or currently open outage duration.
Route/API: `/admin/reports/operational/service-quality`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: Affected Subscribers, Tickets, Work Orders, and Outage Hours cards plus subscriber quality rows.
Permission: `reports:support:read`.
Ownership: Support/field-work facts are Self-Care-owned; outage intervals are authoritative Self-Care projections.
Data freshness/synchronization: Live; events are selected by creation/start time and qualifying outage duration is not clipped.
Plain-English flow: Support demand, field work, and recorded downtime are joined by subscriber to expose service-quality pressure.

## Revenue and service downtime

REPORT: Revenue & Service
Data source: Subscriber, Subscription, Invoice, and CustomerOutageInterval.
Backend query/service: `crm_reporting` financial and outage aggregation.
Transformation/calculation: Sums period invoiced/outstanding values and subscriber outage duration. It does not calculate a downtime credit.
Route/API: `/admin/reports/operational/revenue-service`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: Invoiced, Outstanding, and Customer Outage Hours cards, joined rows, and an unavailable-credit notice.
Permission: `reports:billing:read`.
Ownership: Self-Care-owned billing plus projected outage facts.
Data freshness/synchronization: Live period query.
Plain-English flow: Financial exposure and observed downtime are displayed side by side; compensation remains absent until an authoritative decision owner exists.

## Project and task people performance

REPORT: Project & Task People Performance
Data source: Active ProjectTask and assignee relationships.
Backend query/service: `crm_reporting`.
Transformation/calculation: Expands tasks across assignees, counts total/completed/blocked/overdue work, and averages start-or-creation to completion time. Multi-assignee tasks count once per assignee.
Route/API: `/admin/reports/operational/project-task-performance`; CSV under `/export`.
UI component/template: Shared operational template.
Displayed as: Active Task Records, Assigned People, and Overdue cards plus person-level task rows.
Permission: `reports:support:read`.
Ownership: Self-Care-owned project/task state.
Data freshness/synchronization: Live period query based on task creation.
Plain-English flow: Recorded task assignments and lifecycle timestamps become per-person delivery measures; estimated effort is never presented as actual effort.

## Overall architecture

```text
DATA SOURCE
  native Self-Care tables
  + authoritative Self-Care projections
  + explicit NCC back-office gateway inputs
    -> DATA ACCESS
       owning domain queries and ui.crm_operational_reports
      -> BUSINESS LOGIC
         typed filters, aggregation, pagination, formatting, CSV projection
        -> API / ROUTE
           /admin/reports/* with exact direct-route authorization
          -> SELF-CARE UI
             dedicated templates or operational.html
            -> USER
               authorized cards, charts, tables, filters, pages, and exports
```

The shared operational pipeline is:
`CrmReportQuery -> crm_reporting.get_report -> CrmReportPage -> reports route -> operational.html`.
Date ranges use an inclusive user-facing end date represented internally as an
exclusive next-day boundary. The route first requires a recognized report
permission and then enforces the exact permission declared for the requested
report. Personal performance also requires a matching authenticated person ID.

## Maintenance priorities

1. Shared operational projection: thirteen reports use the same typed query,
   page, permission, template, pagination, and CSV boundary.
2. Billing aggregation: Billing Risk, Revenue/Pipeline, Postpaid, and
   Revenue/Service must retain canonical invoice collectibility and payment
   semantics.
3. Customer/network observations: Subscriber Overview, Online Activity, and
   Service Quality depend on collector-fed RADIUS, bandwidth, and outage facts.
4. Team inbox metrics: CRM Performance, both agent views, and Queue
   Classification are derived from native conversations/messages/assignments,
   never retention engagement records.
5. NCC ownership: complaints/subscribers are native, finance/workforce are
   explicit gateway inputs, and unavailable external facts must remain visible
   rather than being copied or fabricated.

## Explicit exclusions

- The dormant manually populated legacy Quarterly Report and its workbook
  inputs are not migrated or recreated.
- CRM-owned retention notes, dispositions, follow-ups, campaign/outreach state,
  contact preferences, reminders, suppression, and raw engagement records are
  not copied into Self-Care.
- Downtime credits remain unavailable until a canonical compensation decision
  owner exists.
- Project actual-effort accuracy remains unavailable until an authoritative
  actual-effort observation exists.

## Sales agent lead performance

REPORT: Lead Performance
Data source: `Lead`, `Subscriber`, and native `SubscriptionLifecycleEvent` restoration evidence.
Backend query/service: `sales.reports.lead_kpi_report`.
Transformation/calculation: Groups won leads and recorded non-new lead progression by `owner_agent_id`; blocked-contact counts require a linked blocked/suspended subscriber, while brought-back counts require a blocked/suspended-to-active lifecycle event in the selected period.
Route/API: `/admin/reports/sales/leads`; CSV under `/admin/reports/sales/leads/export`.
UI component/template: `templates/admin/reports/sales_kpi.html`.
Permission: `crm:lead:read`.
Ownership: Self-Care-native sales and customer lifecycle owners. CRM engagement history is excluded.

## Sales agent order performance

REPORT: Sales Order Performance
Data source: `SalesOrder` and native payment/order status fields.
Backend query/service: `sales.reports.sales_order_kpi_report`.
Transformation/calculation: Groups orders by `owner_agent_id`, status, currency, total, and amount paid for orders created in the selected period.
Route/API: `/admin/reports/sales/orders`; CSV under `/admin/reports/sales/orders/export`.
UI component/template: `templates/admin/reports/sales_kpi.html`.
Permission: `crm:sales_order:read`.
Ownership: Self-Care-native sales order and finance owners.
