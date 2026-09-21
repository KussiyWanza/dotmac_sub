# Admin workflow guidance

`app.services.admin_workflow_guidance` is the single source for plain-language
Admin help. It explains a workflow but never decides permissions, state, or
financial/service consequences; the route and named domain owner remain
authoritative.

Every staff-facing workflow guide declares its route selector, audience,
purpose, permission-linked actions, ordered instructions, and safety notes. The
Admin layout renders the matching guide's permitted action titles in a Page
Overview modal. `/admin/help` renders the searchable, Admin-sidebar-aligned
catalogue and deep-links directly to the selected page guide.

Help-only sidebar guides and contextual page guides are deliberately separate:
adding a sidebar destination to the Help Center does not add a question-mark
control to that destination.

When an Admin route or workflow changes, update its linked guidance contract in
the same pull request. `scripts/architecture/workflow_guidance_gate.py` is run
by the required **Workflow Guidance Gate** CI job and fails a PR that changes
`app/web/admin` without changing the guidance registry.
