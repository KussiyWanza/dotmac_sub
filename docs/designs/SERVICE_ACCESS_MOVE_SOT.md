# Subscription Service-Access Move Source of Truth

Status: cut over for individual subscription moves

Owner: `service_intent.subscription_nas_assignment`

## Decision

A router change for an existing service is not a normal subscription edit. It
is one service-access transition consisting of all of these facts:

- the exact active subscription currently being served;
- its current provisioning NAS and exact active primary IPv4 assignment;
- one active target NAS;
- one active IPv4 pool explicitly linked to that target NAS; and
- one safe, materialized, currently available IPv4 address in that pool.

The public owner command is
`move_subscription_service_access`. It is the sole workflow permitted to move
an existing subscription's NAS binding. `network.ip_assignment_lifecycle`
remains the canonical IPv4 ledger and served-projection writer; the coordinator
invokes its required flush-only participants inside the same root transaction.

The command does not accept or write offer, price, billing mode, billing cycle,
invoice, recurring add-on, entitlement, or paid-period fields. A service-access
move is operational provisioning work, not a commercial transaction.

## Operator workflow

The admin first opens **Move service access** from the subscription edit page.
The adapter shows the current router and served IPv4, then requires the
operator to select:

1. the new router that will serve this exact subscription;
2. an active IPv4 pool linked to that router;
3. one free address from that pool; and
4. an operational reason.

The interface never automatically selects an address. Preview returns a typed
decision and SHA-256 fingerprint. Confirmation resubmits the exact reviewed
router, pool, address, fingerprint, reason, actor, and idempotency key.

The ordinary subscription Save form renders the router read-only for existing
subscriptions and rejects a forged NAS change at the server boundary. The
legacy bulk migration interface no longer offers NAS or pool targets, and its
service rejects old or forged jobs carrying either target. A future bulk move
must fan out exact typed service-access commands; it may not restore a parallel
NAS/IP writer.

## Transaction and consequences

The owner enters `execute_owner_command` once on a transaction-free session and
locks the subscription, source and target NAS rows, target pool and address,
and relevant assignments. It recomputes the complete preview under those
locks. Changed evidence fails closed.

Within that one transaction it:

1. changes `Subscription.provisioning_nas_device_id`;
2. asks the IPv4 lifecycle participant to deactivate the reviewed old
   assignment and create or link the target assignment;
3. asks the served-projection participant to update
   `Subscription.ipv4_address` to the new desired address;
4. stages durable audit evidence declaring `billing_changed: false`; and
5. stages the existing `ip_assignment.served_projection_repaired` event.

Any required step failing rolls back the NAS binding, IPv4 ledger, served
projection, audit, and event together. After commit, the existing event handler
requests canonical RADIUS reconciliation and disconnects only sessions still
using the old address. External delivery failure is durable and retryable; it
does not create a second decision path.

## Fail-closed conditions

The preview or command refuses the move when:

- the subscription or target NAS is missing or inactive;
- the target NAS is unchanged;
- the pool is inactive, non-IPv4, or not linked to the target NAS;
- the requested address is invalid, outside the pool, absent from inventory,
  reserved, management-owned, routed, device-owned, or already assigned;
- the current exact-service IPv4 assignment is missing or ambiguous;
- current served-IP, RADIUS, or session evidence is not aligned; or
- any fingerprinted evidence changed between preview and confirmation.

Operators reconcile the current IPv4 evidence first when the move is blocked;
they do not bypass the decision through generic edit or bulk migration.

## Migration and verification

Old owners:

- generic admin subscription edit NAS mutation; and
- legacy bulk provisioning migration NAS assignment and
  `IPv4Address.pool_id` repointing.

New owner: `service_intent.subscription_nas_assignment`.

Cutover gates:

- focused behavior tests prove linked-pool validation, atomic success,
  commercial-field isolation, and rollback after a required participant
  failure;
- route/template guards keep the individual adapter on preview/confirm;
- architecture tests restrict the flush-only IPv4 participants to this
  coordinator; and
- architecture tests prevent the generic and bulk bypasses from returning.

Bulk router/IP migration remains explicitly retired until a separate design
defines reviewed per-subscription commands, progress/idempotency semantics, and
safe partial-failure reporting without weakening this owner boundary.
