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
- an immediate model-supported live Huawei autofind observation filtered to the
  exact OLT/F/S/P/serial;
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

Before registration it runs the management-only dependency audit: imported
line/service mappings, referenced DBA profiles, and the OLT TR-069 profile.
Customer internet traffic tables and WAN profiles are not commissioning
dependencies and remain part of the full **Authorize & provision** audit.

The authorization implementation exposes two named capabilities rather than a
public provisioning boolean. `authorize_and_provision_ont` is reachable only
from the exact-assignment command executor.
`register_ont_for_commissioning` is reachable only from this commissioning
owner. Reauthorization also delegates to the exact-assignment command owner;
adapters cannot invoke registration directly.

The boundary is typed end to end:

- `RequestAssignedOntAuthorization` owns assigned-command admission;
- `ExecuteAssignedOntAuthorization` carries the admitted ONT, operation,
  OLT/F/S/P/serial target, force policy, preset, and `CommandContext`;
- `RegisterCommissioningOnt` carries the exact commissioning intent and
  operation;
- `ExecuteOntCommissioning` carries the exact intent, operation, and command
  context from the durable worker adapter, and returns an immutable
  `OntCommissioningExecutionOutcome`;
- `RecordOntCommissioningExternalWriteFailure` carries the same typed identity
  into the fresh-session reliability recorder when worker finalization is lost;
- `OntFsp`, `OntSerialNumber`, and `OntAuthorizationTarget` prevent primitive
  identity bags;
- typed admission, assignment-decision, workflow, and execution outcomes remain
  intact until explicit persistence or transport serialization.

The execution owner repeats the exact active-assignment/PON decision immediately
before device I/O, so assignment drift between admission and worker execution
fails closed without an OLT write.

The restricted `BatchedMgmtSpec` always sets
`internet_config_ip_index=None` and `wan_config_profile_id=None`. Command-batch
validation and result validation both reject internet-config or WAN-config
steps. Commissioning never applies PPPoE, WAN, LAN, Wi-Fi, or saved customer
service intent and never creates an `OntAssignment`.

TR-069 Inform may mark the intent management-ready, but it does not trigger
saved service application unless an active assignment exists.

## External-operation transaction boundary

OLT authorization and management configuration run only after their database
phase has committed. The worker snapshots the required OLT connection and
target values into immutable value objects, closes the transaction, performs
the device operation, and then opens a fresh transaction to persist the
result. Live ORM objects are never passed to an OLT adapter because an expired
attribute read could silently reopen a transaction while the device call is in
progress.

Authorization evidence is persisted immediately after the OLT confirms the
write. If the original session is lost afterward, the task opens a fresh
reliability session and records
`external_write_reconciliation_required`. The reconciler verifies the exact
serial, OLT, canonical F/S/P, ONT inventory identity, recorded operation
payload, and `reconciliation_needed` dispatch. It fingerprints that locked
evidence and stages a linked, retry-bounded management-only redrive with
`authorization_reissue_allowed=false`. The recovery worker then live-verifies
the exact serial, F/S/P, and ONT ID before any management write. Missing or
conflicting evidence, live registration drift, and retry exhaustion fail closed
into durable review state instead of leaving the UI in `authorizing`.

## Concurrency and cleanup

Commissioning admission locks the exact autofind candidate. Immediately before
the first OLT write, the worker reads live autofind and fails closed if the
serial is absent or appears on another F/S/P. Huawei command selection is
model-aware: MA5608T uses `display ont autofind all`, then the parsed observation
is filtered in-process to the requested F/S/P because that shelf rejects
`display ont autofind <F/S/P>`. The cached candidate never substitutes for this
live exact check. Commissioning also disables the legacy automatic “remove old
registration and move” behavior.

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

Authorization, reconciliation, and cleanup all compare the shared canonical
F/S/P representation. A stored board such as `0/1` plus port `13` normalizes to
`0/1/13`; callers must not prepend another frame segment.

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
