# ONT commissioning intent

## Decision

Raw assignment-free “authorize only” is not a supported workflow.

Normal **Authorize & provision** requires one exact active `OntAssignment` whose
modeled PON belongs to the submitted OLT and F/S/P. Assignment-free operational
bootstrap uses the separate `network.ont_commissioning` owner and the
`network:ont:commission` permission.

## Owner and authoritative inputs

`app.services.network.ont_commissioning` owns the temporary intent, its state,
expiry, assignment conversion, and cleanup admission. It consumes:

- the operator's permission, reason, optional work-order/ticket reference, and
  exact selected candidate;
- an immediate live Huawei autofind observation for the exact OLT/F/S/P/serial;
- canonical OLT, ONT, PON, and active-assignment identity;
- config-pack management VLAN, imported GEM/priority, management IPAM, ACS, and
  OLT TR-069 profile;
- the network operation ledger and durable dispatch outbox.

The Huawei OLT and GenieACS supply observations. Neither decides assignment,
customer service, expiry, or cleanup.

## State model

```text
commissioning → authorizing → awaiting_acs → management_ready
      │              │              │
      └──────────────┴──────────────┴──→ failed

management_ready → assigned → provisioned

any active unassigned state at expiry
    → cleanup_pending → cleanup_running → expired
```

`assigned` and `provisioned` are terminal evidence that the normal assignment
owner took control. `failed` stays active until assignment or expiry cleanup,
because the OLT may already contain landed device or management state.

The default expiry is 24 hours and admission rejects a TTL above 72 hours.
There is one active intent per canonical serial.

## Management-only invariant

Commissioning may perform only:

1. exact ONT serial registration on the selected OLT F/S/P;
2. management VLAN service-port creation;
3. IPHOST configuration from management IPAM, or DHCP when the OLT has no
   management pool;
4. OLT TR-069 profile binding;
5. bounded observation of GenieACS readiness.

The restricted `BatchedMgmtSpec` always sets
`internet_config_ip_index=None` and `wan_config_profile_id=None`. Command-batch
validation and result validation both reject internet-config or WAN-config
steps. Commissioning never applies PPPoE, WAN, LAN, Wi-Fi, or saved customer
service intent and never creates an `OntAssignment`.

TR-069 Inform may mark the intent management-ready, but it does not trigger
saved service application unless an active assignment exists.

## Concurrency and cleanup

Commissioning admission locks the exact autofind candidate. Immediately before
the first OLT write, the worker reads live autofind and fails closed if the
serial is absent or appears on another F/S/P. It also disables the legacy
automatic “remove old registration and move” behavior.

The assignment owner locks the ONT and rejects assignment while commissioning
is authorizing, awaiting ACS, or cleaning up. Assignment is allowed only after
`management_ready`. The reconciler then records `assigned` or `provisioned`.

At expiry the reconciler rechecks assignment and identity under lock. If no
device write landed, it closes the intent without device cleanup. Otherwise it
stages a tracked return-to-inventory operation. A landed write without a local
ONT projection remains `cleanup_pending` for manual projection repair; it is
never treated as a no-write expiry. The cleanup worker locks the intent and ONT,
rechecks the exact serial/OLT/F/S/P and absence of assignment, then uses the
canonical inventory-return service to remove service ports, deauthorize the
ONT, remove ACS state, release management IPAM, and restore rediscovery.
Identity disagreement or cleanup failure remains durable `cleanup_pending`
evidence and blocks assignment until reviewed.

## Migration and fallback retirement

The old behavior accepted unassigned requests through the normal authorization
route, performed an OLT write, and failed later when full baseline provisioning
discovered that assignment was missing. Cutover is complete when:

- the normal command admission rejects requests without the exact active
  assignment and PON;
- unassigned UI actions route only to the explicit commissioning confirmation;
- the granular permission exists in seed data and migrations;
- the permanent reconciler and durable commission/verify/cleanup dispatches are
  enabled;
- behavior and architecture tests enforce the management-only boundary.

There is no compatibility fallback to raw assignment-free authorization.
