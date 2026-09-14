# September 14 consolidation validation

The consolidation includes PRs 3134 through 3143 and 3145 through 3149.
Draft PR 2992 is explicitly excluded. PR 3144 is also excluded because PR 3149
replaces its stale Team Inbox implementation. The separate local
`fix/historical-renewal-documentary-invoice` branch is not part of this batch.

## Combined behavior

The release adds Field password recovery, hardens session and CSRF handling,
improves database-transaction evidence, replaces reseller ticket fallbacks,
corrects ticket SLA reporting, repairs legacy prepaid renewal invoices, improves
customer and mobile support-ticket views, reduces unnecessary attendance
polling, provisions PPPoE credentials during activation, adds governed historical
invoice tax correction, delivers realtime support-ticket comments, fixes modal
map layering, and establishes the authoritative Team Inbox Unknown-to-Lead
lifecycle.

## Migration chain

The application migration added by this batch continues the existing linear
chain:

1. `606_project_task_subtasks`
2. `607_inbox_lead_identity_expiry`

Before staging, require the protected consolidation pull-request checks and the
exact merged `origin/main` commit checks to pass. Build that commit once, deploy
its immutable digest to staging, record staging acceptance, authorize that same
digest, and deploy it to production through the protected release workflows.
