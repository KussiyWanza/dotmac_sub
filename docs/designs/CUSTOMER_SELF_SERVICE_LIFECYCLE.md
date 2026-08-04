# Customer Self-Service Lifecycle

Status: implemented

## Contract

```text
customer review
  -> server-owned preview and eligibility
  -> confirmed, idempotent intent
  -> canonical commercial/lifecycle command
  -> billing and access consequences
  -> delivery-mode branch
      -> commercial-only apply
      -> remote reprovision + verification
      -> field fulfillment + verification
  -> verified operational state
  -> Customer 360, portal, reseller, mobile, and timeline projection
```

Sub is authoritative. Web routes, APIs, templates, mobile clients, support
tickets, work orders, RADIUS, and equipment controllers are adapters or
downstream records; none may independently decide the customer lifecycle.

## Owners

| Decision or fact | Owner |
| --- | --- |
| Subscription state, command preview, billing/access impact, delivery mode, relocation qualification and field-fee preview | `service_intent.subscription_lifecycle` |
| Locked command execution and idempotent outcome | `service_intent.subscription_lifecycle_execution` |
| Plan-change request intent and result evidence | `app.services.subscription_changes` |
| Prepaid plan-change pricing/funding evidence | `financial.prepaid_plan_change` |
| Account/subscription access state | `access.subscription_lifecycle` |
| RADIUS session projection | `network.radius_sessions` |
| Connection/outage diagnosis | `network.connection_health` |
| Support-to-field handoff | `support.ticket_work_order_handoff` |
| Work-order execution | `operations.work_order_commands` |
| Provisioning readiness/result | `operations.provisioning_lifecycle` |
| Deferred change payment admission, fulfillment release, and verified finalization | `service_intent.subscription_change_execution` |
| Cross-surface read projection | `ui.portal_account_health_projection` |

## Change classes

Every compatible upgrade and downgrade uses a reviewed service-change preview
and the subscription lifecycle command owner. The subscription, exact
financial adjustment, access/profile consequence, request evidence, audit, and
event must converge in one idempotent outcome.

`plan_family` is commercial merchandising policy and never proves that a site
visit or access-network migration is required. The service-change owner derives
one delivery mode from provisionable catalog and current-service facts:

- `commercial_only`: no network intent changes; apply without provisioning;
- `remote_reprovision`: the access medium is unchanged but speed/profile intent
  changes; persist the confirmed intent, then execute and verify remote
  provisioning without a ticket or work order;
- `field_migration`: the physical access medium or another field-only fact
  changes; persist the confirmed intent, then create fulfillment scope and a
  work order.

A service-address change is always `field_migration`, including when the
customer keeps the current offer. For fixed-wireless/radio access the owner
must qualify the exact target address and resolve a nonzero one-time field fee
from the catalog offer selected by
`projects.wireless_relocation_offer_id`. Missing coordinates, an ineligible
qualification, an absent/inactive offer, or a missing/nonpositive one-time
price blocks confirmation. Templates and mobile clients render this result;
they never infer serviceability or calculate the fee.

The field fee is separate from recurring-plan proration. Confirmation persists
the target address, qualification evidence, exact fee offer/amount/currency,
and quote fingerprint on `SubscriptionChangeRequest`. A priced relocation
enters `awaiting_payment`; the current subscription offer and service address
remain unchanged. Fee settlement must precede the service-order/work-order
handoff, and only verified field provisioning may request the final
subscription/address change.

Support tickets are exception/triage collaboration only. They are not a
mandatory step in a planned service change. If support becomes involved, the
ticket links structurally to the service-change intent; it does not own it.
Field completion does not itself change the plan: verified provisioning
evidence must satisfy the service-change owner before the subscription command
is executed.

Vacation hold and resume use the subscription lifecycle owner with the
customer-hold policy and exact enforcement-lock evidence. Device reboot and
Wi-Fi changes remain network command intents scoped to the customer's exact
subscription and assigned device; page rendering never performs equipment
polling or writes device state.

## Projection

`ui.portal_account_health_projection` is the shared first-view read contract.
For every operationally-current service it shows lifecycle, access, session,
connection/outage evidence, freshness, next action, and any pending plan or
network change. Customer Portal, Reseller Portal, Customer 360, and mobile
consume the same DTO.

## Migration and retirement

Completed in this slice:

- Customer 360 consumes the canonical service-health strip.
- Active `SubscriptionChangeRequest` state is batch-projected into Account
  Health and mobile.
- Every compatible customer upgrade and downgrade is classified by provisionable
  facts through `service_intent.subscription_lifecycle_execution`; the portal
  adapter retains only customer scope, catalog exposure, preview display, and
  transport-error mapping.
- Commercial-only changes apply immediately. Remote and field changes persist a
  reviewed, idempotent `SubscriptionChangeRequest` without changing the current
  subscription, opening a ticket, or creating unverified billing effects.
- The former plan-family filter, support-ticket fallback, migration endpoint,
  and duplicate migration offer list are retired.
- Customer self-service API routes are `/service-change`; the former
  `/plan-change` paths are retired without compatibility aliases.
- Customer Portal, Reseller Portal, `/api/v1/me`, reseller API, and mobile all
  use the same address-scoped preview and confirmation owner. The reseller
  adapter proves managed-account scope before invoking it.
- Address-only relocation is supported without manufacturing a plan change.
- Fixed-wireless/radio relocation is serviceability-qualified and catalog-
  priced, and confirmation remains `awaiting_payment` without prematurely
  changing the subscription.
- Priced relocation confirmation creates and structurally links the exact
  issued invoice. Canonical paid-invoice allocation evidence advances the
  locked request from `awaiting_payment` and creates one idempotent service
  order plus native work order; invoice status or memo text alone is never
  settlement evidence.
- Relocation work-order completion is now an explicit provisioning-readiness
  input. The final address/offer application is admitted only from the
  `activated` readiness decision for the exact linked service order, and the
  request preserves invoice, payment, service-order, work-order, and readiness
  identifiers for audit and repair.
- The execution owner exposes deterministic drift audit and idempotent repair
  for paid-but-unreleased and verified-but-not-finalized requests; it never
  repairs from memo text, portal state, or unverified provider payloads.
- Remote reprovisioning now resolves exactly one catalog-linked target RADIUS
  profile and one active subscription credential. Confirmation records the
  target without changing either the live offer or desired credential profile.
  Only execution-time price confirmation stages the desired profile, and
  completion is accepted only when the exact subscription-scoped RADIUS user
  carries that profile with a sync watermark after the provisioning request.
  The structural profile/user links and verification time remain available for
  replay, drift audit, and repair.
- Operator-triggered remote reprovisioning recomputes the canonical plan-change
  price and funding decision before network I/O. A changed amount is persisted
  as review evidence and requires explicit confirmation; insufficient funding
  remains a billing-specific, retryable state. After confirmation, RADIUS is
  projected before commercial finalization. If finalization does not commit,
  the execution owner restores the previous desired RADIUS profile and
  reprojects it before returning the request to `provisioning`; if the
  commercial owner already committed, recovery completes the request instead.
  Audit rows are staged only with the safe review, compensation, or completed
  state they describe.
- Vacation hold and resume are explicit subscription lifecycle commands. The
  lifecycle policy owner resolves duration, annual-use, cooldown, active-lock,
  and subscription-status eligibility from canonical settings and exact
  `customer_hold` lock history. Customer, admin, and automatic-expiry adapters
  now delegate to the same locked, reviewed-head, idempotent command and retain
  the exact enforcement-lock identifier in the outcome.
- Customer reboot and Wi-Fi updates now enter one subscription-scoped command
  boundary. It proves the authenticated subscriber, active subscription, and
  exact active non-UISP ONT assignment before invoking the network operation
  ledger. Web and `/api/v1/me` expose the same typed command, status,
  subscription, device, operation, and message outcome; mobile provides both
  actions and renders that canonical outcome without inferring device state.
- Superseded customer device-command wrappers and their duplicate cooldown and
  validation decisions are retired; web routes, API, and mobile enter the
  canonical scoped owner directly.
- Operators now have a permission-gated service-change reconciliation surface.
  Its read-only inspection reports bounded canonical drift with a reviewed
  evidence head. Repair requires that exact head, a durable idempotency key,
  actor, and reason, then resumes only from structural payment, fulfillment,
  RADIUS, or provisioning evidence. A crash after payment settlement but before
  fulfillment release is explicitly detectable and repairable.

The lifecycle migration is complete. Further changes extend this contract and
must preserve these owners rather than introduce compatibility writers.

No compatibility response or fallback will be retained after each in-repository
consumer is migrated and the cutover gate is green.
