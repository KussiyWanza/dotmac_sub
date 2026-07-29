# Selfcare Pipeline Settings

## Authority

Selfcare's native `sales.service` owns pipelines, stages, stage ordering, and
lead assignments. The admin web routes and templates are adapters. They do not
proxy writes to CRM and do not reproduce a second pipeline lifecycle.

The canonical settings route is:

```text
/admin/sales/pipelines-settings
```

The former `/admin/sales/pipelines` and `/admin/sales/pipelines/new` GET routes
are compatibility redirects. Existing legacy POST routes remain temporary
aliases so an in-flight form cannot fail during deployment. New links and
forms use only the canonical route.

## Governed stage presentation

`app.services.sales.pipeline_configuration` owns the presentation vocabulary
stored under the versioned `pipeline_stage_presentation_v1` metadata key:

- `stage_type`: `standard`, `closed_won`, or `closed_lost`;
- `color`: a six-digit hexadecimal color;
- `icon`: an optional key from the registered icon vocabulary.

This metadata classifies and renders a stage. It does not independently
transition a Lead or grant access. Existing stages without the metadata remain
readable: the two conventional terminal names are inferred for display and
all other stages are standard.

## Ordering and concurrency

Drag-and-drop sends the complete ordered stage identifier set to
`PipelineStages.reorder`. The owner locks the pipeline's stage rows, rejects
duplicate or incomplete/stale identifier sets, assigns contiguous zero-based
orders, and commits once. A normal submit button preserves manual operation if
drag-and-drop is unavailable.

The Kanban query reads the same governed presentation contract, so saved color,
icon, type, and order changes are visible on the Pipeline Board immediately.

## Lifecycle and safety

- Pipeline and stage deactivation is soft; existing data remains intact.
- Pipeline and stage lists include inactive records.
- Stage lead counts and pipeline lead counts are observations of active Leads.
- Bulk overwrite requires explicit browser confirmation.
- Every route requires `crm:lead:write`; the Pipeline Board keeps its
  `crm:lead:read` requirement.
- Domain validation remains in the native sales services. The web adapter maps
  failures to an operation-failed notification.

## UI states

The page keeps the recommended stages visible even when no pipelines exist.
It provides a dedicated first-pipeline empty state, instant client-side
name/status filtering, collapsible management cards, confirmation prompts, and
toast outcomes. Pipeline creation redirects to the canonical Pipeline Board
with the new pipeline selected.
