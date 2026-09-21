# September 8 consolidation validation

The consolidation includes PRs 2976–2988 and 2993–3001 that were open when
the batch was selected. PR 2992 is excluded. The saved managed-connectors
change is included using the newer installation service and existing UI tests.

## Combined behavior

AI-owned conversations require explicit human takeover. Takeover must satisfy
team membership, online presence, FIFO order, and global agent capacity. Failed
assignment rolls back the AI stop. AI handoff provenance passes through both
automatic assignment and queue admission. Queue notices retain lifecycle
suppression and handoff notices; outbound AI notices retain ownership checks.

Classifier failures retain reason and failure-kind evidence, retry limits and
exhaustion, while the newer response policy retains expected facts, affect
assessment, and question planning. Readiness, operational logging, and payment
failure metrics remain separate observations.

The sales Quote guide includes both recipient selection and staff payment review,
so one route does not hide the other workflow's instructions. Capacity-change
queue admission preserves the typed agent-selection evidence in its routing event.
Concurrency checks scope their assertions to their own team while sharing the
migrated test database. Quote-payment unit fixtures review persisted values,
matching the staff review owner's read path.

## Database acceptance

The application migrations form one linear sequence from
`583_staff_expense_requesters`, as required by the migration sequence gate:

1. `584_customer_backed_quote_delivery`
2. `585_quote_payment_review`
3. `586_inbox_sla_rules`
4. `587_field_request_requester_history`
5. `588_team_inbox_queue_correctness`

The unpublished branch migrations were renumbered and linked before release;
their schema operations and individual locking, retry, and downgrade contracts
are unchanged. The former merge-only revision is removed. An existing developer
database using the superseded branch revisions must be recreated as a disposable
test database; do not stamp it as the new sequence or treat it as release evidence.

Before staging, rehearse both a fresh migrated PostgreSQL/PostGIS database and
the deployed predecessor `583_staff_expense_requesters` to the combined head.
Verify the existing composed-module heads as well as the final application
head using `make test-integration` with an explicit disposable test database.
Do not interpret SQLite unit tests as database acceptance.

Release through feature-branch validation, protected merge to `main`, validation
of the exact `origin/main` commit, the automated rolling version PR, one candidate
build, staging acceptance, and authorization and production deployment of that
same digest. Follow `docs/runbooks/STAGING_PROMOTION.md`; Michael must name the
staging and production hosts before deployment.
This document records required gates, not evidence that they have passed.


## Ownership validation corrections

Inbox SLA configuration and scheduled evaluation now use the registered public
owner transaction. Warnings include clocks whose response deadline has not yet
elapsed. Policy audit and clock transition events are staged with their state.
The configuration planner uses typed sanitized source references and remains
dry-run only. The new quote-review permission is `sales:quote:review` in seed,
migration, authorization, and tests.

Queue preflight in the worker is read-only. A rejected delivery is handed to
`settle_rejected_queue_delivery`, which locks current conversation/queue state
before the notification, revalidates, and commits cancellation and delivery
ledger evidence together. If the decision has changed, it returns the message
to the queue for a fresh claim without contacting a provider. Allowed delivery
continues to hold the existing lifecycle locks through provider dispatch.
