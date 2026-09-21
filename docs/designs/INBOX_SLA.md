# Inbox SLA rule definition and enforcement

Owner: `communications.inbox_sla`

The evaluator's reliability classification is owned by
`observability.task_reliability`: failures are logged and the next scheduled
sweep re-evaluates eligible clocks. Clock locks and persisted warning/breach
evidence make repeated evaluation idempotent; the transport does not blindly
autoretry the task.

Selfcare Inbox SLA clocks are separate from the retired external inbox conversation
history and from customer service-level availability scoring.  A clock is
created only when an active Selfcare policy and matching rule exist.  The
default policy is an explicit fallback; there is no hard-coded runtime target.

Rules match service team, channel, and integer Inbox priority.  More specific
matches win; a default policy wins only when specificity is tied.  The first
customer inbound event starts one clock.  Duplicate inbound events do not
restart first response.  Only an outbound message carrying a human Inbox
person identity or human sender type satisfies response timing; AI and system
messages do not.  Resolution completes the clock.  Reopening clears only the
completion fact and preserves prior breach evidence.

Business calendars use UTC storage and a policy timezone, defaulting to
`Africa/Lagos`, Monday-Friday 09:00-17:00, with explicit ISO holiday dates.
The available historical evidence did not prove a next-response or pause contract, so
next-response is optional and pause/resume remains an explicit future command;
the importer never invents either value.

The configuration planner accepts sanitized JSON and explicit team/channel/priority
maps.  It is dry-run by default and rejects unresolved mappings.  It must not
copy historical conversations, messages, users, customers, breach history, or
credentials.  Operators must retain the sanitised dry-run output and a
configuration backup before applying a reviewed import.


## Command ownership and retries

`communications.inbox_sla` owns `save_policy`, `activate_policy`, and
`evaluate_due_clocks`. Each public command enters `execute_owner_command` once
on a transaction-free session. HTTP and scheduled adapters only open/close
sessions, translate typed commands, and serialize typed outcomes. Conversation
lifecycle helpers remain flush-only participants in their existing owner.

Policy writes serialize default selection with a PostgreSQL transaction advisory
lock, including an empty policy table. Duplicate names are rejected; repeated
activation with the same value is a no-op. Administrator scope and actor evidence
must agree. Policy changes stage typed actor audit evidence and the versioned
`inbox.sla.policy_changed.v1` event in the same transaction.

The sweep locks a bounded set of clocks with `SKIP LOCKED`, oldest evaluation
first. It includes clocks before their deadline so warnings can be recorded on
time. Clock transition evidence and `inbox.sla.clock_changed.v1` events commit
together. Repeated sweeps reuse persisted warning/event identity; a failure
rolls back the entire sweep and the next schedule retries the current facts.

The planner is deliberately dry-run only. Its sanitized input uses `policies`
containing `name` and `rules`, with `source_team_id`, `source_channel`, and
`source_priority` references. Mapping files contain `team`, `channel`, and
`priority` string maps. Unknown fields and unresolved mappings are refused.
All policies are reported as skipped because this planner does not write them;
`--apply` remains unavailable until an approved import command is implemented.
