# Configurable ERP operational sync

Selfcare owns projects, project tasks, tickets, and service work orders. DotMac
ERP receives read-only operational projections for finance and resource context.
ERP never becomes a second workflow writer for those source records.

The `dotmac.erp` installation exposes one
`erp.operational_context.sync.v1` binding. Its reviewed scope contains an
allowlist of enabled domains (`projects`, `project_tasks`, `tickets`, and
`work_orders`); its policy fixes the destination to
`/api/v1/sync/sub/bulk` and bounds batch size. Administrators configure all
profiles from `/admin/integrations/erp`. They cannot enter arbitrary URLs.

Each domain advances an independent `(updated_at, id)` watermark only after ERP
accepts the complete bulk request without errors. Replays are safe because ERP
upserts source identities within the authenticated organization. Project tasks
require the project profile and are sent after projects and tickets.

Project-task batches require ERP bulk contract version 2. A missing or older
version fails closed without advancing any watermark, making either deployment
order recoverable. Deploying ERP first is still the normal rollout.

The dedicated scheduler is enabled only while the capability binding is
enabled. The same page provides a manual run action. Disabling every profile
disables only this capability, leaving inventory, material support, payables,
attendance, and other ERP capabilities untouched.
