# Configurable ERP operational sync

Self-Care owns projects, project tasks, tickets, and service work orders. DotMac
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

ERP responds with `contract_version: 2` plus separate project, ticket,
project-task, and work-order counts. The typed client rejects a missing or older
response before any watermark advances. Item errors use ERP's compatibility
field `crm_id` for the Self-Care source UUID; the typed client converts it to
`source_id` internally. Deploy ERP and its migration first, then enable the
Self-Care capability.

The dedicated scheduler is enabled only while the capability binding is
enabled. The same page provides a manual run action. Disabling every profile
disables only this capability, leaving inventory, material support, payables,
attendance, and other ERP capabilities untouched.

## ERP projection and expense use

ERP upserts every entity by authenticated organization, entity type, and the
Self-Care UUID. Tasks are stored as ERP project tasks and retain their project,
optional parent-task, and optional ticket links. Imported records populate the
Project, Ticket, and Task choices on both the employee expense-claim form and
the Finance expense form. ERP remains the owner of the expense records; it does
not become the owner of the source project or ticket lifecycle.

## Verification

- `tests/test_dotmac_erp_domain_sync.py` verifies typed payload construction,
  watermarks, no-op sweeps, and error behavior in Self-Care.
- The ERP repository's
  `tests/integration/test_sub_operational_sync_v2.py` sends the exact wire shape
  through the real ERP API route and service on migration-built PostgreSQL. It
  verifies all three requested entities, their links, both expense form data
  sources, and idempotent replay. The success response is produced by ERP, not
  invented by a Self-Care mock.

## Limitations

- The incremental feed projects status changes but does not delete an ERP copy
  when a source row disappears.
- Parent tasks must be processed before children; the sender replays missing
  ancestors to guarantee this ordering.
- A batch with any ERP item error is retried from the same Self-Care watermarks.
  ERP may already hold successful items, so its source-ID upsert must remain
  idempotent.
