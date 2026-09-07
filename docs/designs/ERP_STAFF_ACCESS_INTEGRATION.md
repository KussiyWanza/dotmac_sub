# ERP staff-access integration

Status: implemented compatibility and reconciliation contract

Owner: `auth.erp_staff_access`

ERP owns employee status and approved leave applications. Selfcare stores only
rebuildable projections and enforces their consequences locally. A leave
projection makes system-user writes read-only for the inclusive ERP leave date
range; it does not remove roles or permissions. An inactive account-status
projection may close the local staff credential and sessions, but it does not
remove independent local RBAC assignments.

## Transport contract

ERP publishes the flat, versioned payloads
`staff.leave_restriction.v1` and `staff.account_status.v1` through service hooks.
Selfcare validates their exact typed shapes only after HMAC verification and
uses `X-Dotmac-Delivery` as the durable event identity. ERP leave dates are
inclusive local dates. ERP supplies the organization's validated IANA timezone,
and the adapter converts both local midnight boundaries to UTC for Selfcare's
half-open timestamp query. Receivers default a missing timezone to UTC for
backward compatibility with already queued v1 events. An event with no Selfcare
account mapping is acknowledged as `unmapped` and changes no local state. An
invalid timezone or stale account UUID fails closed.

## Freshness and repair

The enabled `erp.staff_access.reconcile.v1` capability reads both entities from
ERP's `/api/v1/sync/sub/staff-access/projection` endpoint every 15 minutes. This
durable interval is the bounded repair path when a service hook is absent,
delivery is missed, or a deployment starts after a wall-clock run. The task
refuses a response at the bounded 500-row limit instead of treating a possibly
truncated page as complete. It maps the typed snapshot into the same owner
command used by webhook ingress. Monotonic source versions make delivery
retries idempotent. Each scheduled run has its own idempotency identity, so an
unchanged ERP snapshot can still repair local drift such as an administratively
reactivated ERP-inactive account.

The shared ERP bootstrap binds every production capability before enabling the
installation and validates each binding independently. Staff-access validation
performs a bounded one-row projection read and reports `sub:staff_access:read`
when ERP rejects the credential; operational-sync validation sends an empty,
idempotent batch and reports `sub:domain:write`. A valid inventory credential
therefore cannot mask either missing capability scope.

The authoritative inputs are ERP employee rows and approved leave applications.
Neither webhook nor reconciliation may create, approve, or infer a leave record.
Drift is visible as task failure, a mapping/version domain error, or a local
projection version behind the ERP snapshot. The repair owner is
`reconcile_staff_access_snapshot`.
