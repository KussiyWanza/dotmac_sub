# September 13 consolidation validation

The consolidation includes every pull request that was open when the batch was
selected except draft PR 2992. The included PRs are 3125, 3126, 3127, 3128,
and 3129. PR 2992 is explicitly excluded and none of its commits are part of
the consolidation.

## Combined behavior

Team Inbox gains lazy customer and lead identity linking with canonical contact
context and repair evidence. Field technicians gain expense history and detail
views. ERP workforce onboarding provisions the governed staff and Nextcloud
Talk identity mapping. Project templates gain revisioned task and subtask plans.
Permission-denied expense payment delivery can be previewed and safely requeued
with its original idempotency key after ERP confirms that no payment exists.

## Migration chain

The application migrations form one linear sequence after the existing
material-request cancellation migration:

1. `604_material_cancel_pending`
2. `605_erp_staff_talk_mapping_scope`
3. `606_project_task_subtasks`

The unpublished project-task migration was renumbered from 605 to 606 and linked
after the ERP workforce migration. Its schema operations and downgrade contract
are otherwise unchanged. A disposable database that used the superseded
unpublished revision identifier must be recreated rather than stamped forward.

Before staging, require the protected pull-request checks and the exact merged
`origin/main` commit checks to pass. Build that commit once, deploy its immutable
digest to staging, record staging acceptance, authorize that same digest, and
deploy it to production through the protected release workflows.
