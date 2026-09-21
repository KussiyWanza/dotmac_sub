# Provisioning Lifecycle Source of Truth

Status: implemented by migration `390_provisioning_lifecycle_sot`

## Decision

`app.services.provisioning_lifecycle` owns provisioning readiness decisions.
`app.services.service_order_lifecycle` remains the sole writer of terminal
`ServiceOrder.status` transitions to `active` or `failed`. A provisioning
workflow records technical execution facts; it does not decide that customer
service is ready or write terminal status.

The lifecycle is deliberately two phase:

1. A terminal `ProvisioningRun` event asks the owner to evaluate readiness.
2. The owner locks the exact service order and run, records normalized checks,
   and either blocks, fails, or requests activation.
3. A ready decision emits `service_order.activation_requested` with the exact
   `service_order_id` and `readiness_decision_id`; the subscription remains
   pending.
4. Existing IP, RADIUS, and NAS activation projections execute. A projection
   failure fails event delivery and remains retryable without presenting the
   subscription as active.
5. Only after those projections succeed does the handler ask the readiness
   owner to confirm that exact service order. Confirmation records an
   `activated` decision and asks the service-order lifecycle owner to set the
   order active, activate the subscription, and emit `service_order.completed`
   plus the canonical `subscription.activated` fact.

Subscription-wide completion and direct admin activation are not fallback
paths. They are removed.

## Authoritative relationships

`ServiceOrder.project_id` is the native installation-project link.
`ServiceOrder.activation_project_task_id` is the exact project task whose
completion gates a new installation. Field work remains related through the
existing native contract:

```text
SalesOrderLine 1 ── 0..1 ServiceOrder
                          │
                          ├── 0..1 Project
                          │        │
                          │        └── 0..n ProjectTask
                          │                    │
                          └── activation task └── 0..n WorkOrder
```

One activation task may require zero visits (no field dispatch) or multiple
visits. If visits exist, every active work order must be terminal and at least
one must be completed. Work-order completion is evidence; it does not itself
complete a project task or activate service.

For an existing-subscription relocation, the canonical relationship is
`SubscriptionChangeRequest.service_order_id` plus its exact
`SubscriptionChangeRequest.work_order_id`. This branch does not manufacture a
new-install sales order or project. The readiness owner requires that linked
native work order to be completed, along with the normal provisioning-run and
IP-assignment checks. Its activated decision is the only evidence accepted by
`service_intent.subscription_change_execution` for the final address/offer
command.

## Readiness facts and decisions

`ProvisioningReadinessDecision` is append-only and idempotent on
`CommandContext.command_id`. Reusing a command id for another service order or
provisioning run fails closed with `command_replay_conflict`; it cannot return
evidence from a different scope. Each decision owns one row per named check in
`ProvisioningReadinessCheck`:

- `provisioning_run`: the exact run succeeded or failed;
- `project_binding`: a new install resolves to the subscriber's active project;
- `activation_task`: the bound project task is `done`;
- `field_work`: linked native work orders are complete, or no visit was needed;
- `ip_assignment`: an active assignment exists for the exact subscription.

Decision states are `blocked`, `activation_requested`, `activated`, and
`failed`. Facts are observations owned by their domain services. Only this
owner turns them into an activation consequence.

## Connectivity boundary

This change does not promote the shadow connectivity reconciler and does not
change IPAM, RADIUS, NAS, or ONT authority. The active IP assignment is consumed
as a readiness observation. The existing activation projections remain in
place, but failures now propagate to the event store instead of being logged
and treated as customer success. The connectivity cutover described in
The service/network projection contract remains independently guarded in
`docs/FINANCIAL_ACCESS_ENFORCEMENT.md`.

Dotmac's subscriber activation projection is IPv4-only. Activation, service-
order confirmation, and resume therefore call the IPv4 allocation participant;
they do not require or attempt an IPv6 allocation. A missing IPv6 pool cannot
block PPPoE/RADIUS projection for an IPv4 service. The participant flushes into
the event delivery transaction so a later RADIUS or NAS failure does not leave
a separately committed allocation.

Remote same-medium service changes use the existing RADIUS control plane rather
than manufacturing a field service order. The service-change execution
coordinator asks `access.radius_state` to stage the exact catalog-linked target
profile on the exact subscription credential. It does not change the live
offer. Finalization requires a structurally linked `RadiusUser` for that
subscription whose observed profile matches and whose `last_sync_at` is at or
after the staging watermark. Ambiguous profiles, credentials, users, stale
syncs, and mismatched profiles fail closed and remain repairable from the same
canonical evidence.

Creating a pending subscription stages service intent only. Credential lookup
is scoped to that exact subscription: creation must not rename, re-secret, or
re-profile a credential bound to another live service merely because both
services belong to the same subscriber. If no exact credential is staged,
activation mints and binds one for the new subscription, and the subscription
login follows that exact credential identity. `access.pppoe_credentials` owns
the typed, idempotent, flush-only credential command. The lifecycle owner calls
it before staging active status or the activation event, so an unavailable or
ambiguous credential leaves the subscription pending rather than presenting an
active service that cannot authenticate.

## Migration and repair

Migration 390 adds the native links and append-only evidence tables. It
backfills a project from matching native `sales_order_id`, then quote identity,
and binds the `power_splicing_activation` task only when the match is unique.
Ambiguous or missing links remain null and fail closed during readiness.

New sales-order service orders resolve and persist both links at creation.
Repeating that staging operation repairs previously missing unambiguous links.
Customer project and service-order pages read the latest persisted decision;
they do not reimplement readiness policy.

## Retired authority

The following old decision paths are removed in the same cutover:

- provisioning-completed events assigning `ServiceOrder.status = active`;
- provisioning-failed events assigning order status outside the owner;
- subscription activation completing every in-flight service order for a
  subscription;
- `ServiceOrders.update` activating a pending subscription from a raw status
  edit.

Legacy appointment and provisioning-task storage is not activation authority.
Its customer/field scheduling replacement remains a separate coherent domain
slice; it may not regain activation decision rights while it is retired.

## Verification

- `tests/test_provisioning_lifecycle.py` covers blocked readiness, two-phase
  activation, confirmation guards, and event replay.
- `tests/architecture/test_provisioning_lifecycle_sot.py` enforces one terminal
  order-state writer and thin event adapters.
- `tests/architecture/test_pppoe_activation_boundary.py` enforces activation-
  owned credential creation and IPv4-only connectivity projection.
- SOT manifest contract validation checks authority, transactions, errors,
  events, projections, migration state, and source references.
