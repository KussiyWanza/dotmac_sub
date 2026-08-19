# Self-Care CRM report capability matrix

Status: implementation and shadow-verification contract

Owner: `ui.crm_operational_reports` for report projections; the domain services
listed below retain ownership of every underlying fact.

Detailed source-to-UI handover documentation is in
`docs/designs/CRM_REPORT_DATA_FLOW_GUIDE.md`.

The legacy manually populated Quarterly Report and all CRM retention notes,
dispositions, follow-ups, campaign/outreach history, and raw engagement records
are intentionally outside Self-Care. The supported NCC complaints workbook is
unrelated and remains native.

| Capability | Self-Care owner/query | Route | Permission | UI/test state |
| --- | --- | --- | --- | --- |
| NCC complaints and Tuesday weekly XLSX delivery | `compliance.ncc_complaints_reporting`; `communications.ncc_weekly_delivery` | `/admin/reports/ncc-complaints`; exact scheduled artifact under `/ncc-weekly-runs/{run_id}/download` | `provisioning:read`; configuration requires `notification:write` | Native page/export plus configurable Tuesday schedule, To/CC/BCC/body/sender parity, durable artifact and run evidence; disabled until controlled cutover |
| NCC regulatory pack | `ncc_regulatory_pack` | `/admin/reports/ncc-pack` | `provisioning:read` | Existing native page/PDF tests |
| Network infrastructure | `crm_reporting.network_infrastructure_facts` composed by `web_reports.get_network_report_data` | `/admin/reports/network` | `reports:network:read`; export separately gated | Repaired totals, observed ONT status, PON/fibre/FDH facts |
| Subscriber overview | `crm_reporting.subscriber_segment_facts` plus customer/usage owners, composed by `web_reports.get_subscribers_report_data` | `/admin/reports/customers` | `customer:read` | Repaired plan/region/ticket cohorts and pagination |
| Churned subscribers | `subscriber_growth` plus `crm_reporting.subscription_churn_reason_counts` | `/admin/reports/churn` | `customer:read` | Repaired reason breakdown; CRM retention records excluded |
| Technician performance | `provisioning_managers.technician_report_stats` and `recent_completed_appointments` | `/admin/reports/technician` | `reports:support:read` | Repaired completion semantics and date-consistent export |
| Online activity | `crm_reporting`, native RADIUS owner | `/admin/reports/operational/online-activity` | `customer:read` | Subscriber/status/last-activity page, export, and empty state |
| Subscriber billing risk | `crm_reporting`, native customer/billing owners | `/admin/reports/operational/billing-risk` | `reports:billing:read` | Facts only; no copied CRM engagement state |
| Retention-risk queue preview | `crm_api.billing_risk_rows`, native customer/billing owners | `/admin/customer-retention` | `reports:billing:read` | Visible from Reports Hub; same retention tracker layout and native billing-risk profile drill-down; CRM engagement, follow-up, pipeline, and outreach state remain unavailable |
| Lead performance | `sales.reports.lead_kpi_report` | `/admin/reports/sales/leads` | `crm:lead:read` | Agent won/contacted/recovery KPI table and CSV; native evidence only |
| Sales order performance | `sales.reports.sales_order_kpi_report` | `/admin/reports/sales/orders` | `crm:sales_order:read` | Agent order/status/value KPI table and CSV |
| Subscriber revenue/pipeline | `crm_reporting`, Invoice/Payment owners | `/admin/reports/operational/subscriber-revenue` | `reports:billing:read` | Period-filtered page/export |
| Postpaid customers | `crm_reporting`, customer/billing owners | `/admin/reports/operational/postpaid-customers` | `reports:billing:read` | Native page/export |
| CRM performance | `crm_reporting`, `team_inbox_metrics` | `/admin/reports/operational/crm-performance` | `reports:support:read` | Native team page/export |
| Administrative agent performance | `crm_reporting`, `team_inbox_metrics` | `/admin/reports/operational/agent-performance` | `reports:support:read` | Native searchable agent page/export |
| Personal agent performance | `crm_reporting`, signed-in principal scope | `/admin/reports/operational/my-performance` | `reports:support:read` | Fail-closed personal page/export |
| Operations SLA violations | `crm_reporting`, Ticket/Project/ProjectTask due facts | `/admin/reports/operational/operations-sla` | `reports:support:read` | Period-filtered page/export |
| Queue wait/classification | `crm_reporting`, native Inbox queue and recorded metadata | `/admin/reports/operational/queue-classification` | `reports:support:read` | Unclassified remains explicit |
| Subscriber lifecycle | `crm_reporting`, customer/subscription owners | `/admin/reports/operational/subscriber-lifecycle` | `customer:read` | CRM retention records explicitly excluded |
| Subscriber service quality | `crm_reporting`, Ticket/WorkOrder/outage owners | `/admin/reports/operational/service-quality` | `reports:support:read` | Period-filtered page/export |
| Revenue/service downtime | `crm_reporting`, Invoice and customer-outage owners | `/admin/reports/operational/revenue-service` | `reports:billing:read` | Credit exposure is unavailable, not estimated |
| Project/task people performance | `crm_reporting`, ProjectTask assignment owner | `/admin/reports/operational/project-task-performance` | `reports:support:read` | Actual-effort accuracy omitted until an observation owner exists |

## Explicit exclusions and remaining authority gates

- The dormant CRM Quarterly Report is not migrated, linked, or recreated.
- CRM retention state remains CRM-owned. Self-Care reads no retention tables and
  does not offer retention mutations.
- Downtime credit exposure remains unavailable until a canonical compensation
  decision record exists. Revenue and outage facts are displayed separately.
- Project effort accuracy remains unavailable until actual effort is captured by
  an authoritative owner; estimates are not presented as actual work.
- CRM source routes remain during shadow verification. Their deletion requires
  the traffic, parity, cutover, rollback, and retirement gates in
  `CRM_WEB_RETIREMENT.md`.
