# Fiber technician journey gap list

Companion to `NETWORK_SUPPORT_GAP_LIST.md` (which covers the NOC/support-desk
loop). This list walks the fiber technician journey — prepare → locate →
diagnose → work → test → close — and records, per gap, what exists, the owning
service under the source-of-truth standard, and the fix. Verified against the
working tree on 2026-07-31.

## Status summary

| ID | Gap | Status |
|---|---|---|
| **F1** | Field app blind for troubleshooting (no trace/optical) | **Closed 2026-07-31** |
| **F2** | No feedback loop on splice proposals | **Closed 2026-07-31** |
| **F3** | Internal completion not gated on fiber as-built evidence | **Closed 2026-07-31** (project work orders) |
| **F4** | Vendors cannot record structured splices | **Closed 2026-07-31** |
| **F5** | No tube/core color language in capture or review | **Closed 2026-07-31** (derived) |
| **F6** | ODF detail invisible to technicians | **Closed 2026-07-31** (trace annotation) |
| **F7** | No splice plan / cut sheet (design-first flow) | **Closed 2026-07-31** |
| **F8** | Test acceptance is self-asserted | **Closed 2026-07-31** |
| **F9** | Install last mile not captured from the field | **Closed 2026-07-31** |
| **F10** | Field cannot record cable/closure/damage | **Closed 2026-07-31** |
| **F11** | Buffer tube not a physical entity | **Resolved 2026-07-31** (derivation + tube-scoped ops) |
| **F12** | Evidence map covers only staged-verification jobs | **Closed 2026-07-31** |

## Closed in this change (2026-07-31)

- **F1** — `GET /field/fiber/customer-trace`: job-scoped customer traces from
  the canonical owner (`app.services.fiber_topology.trace_fiber_subscription`)
  plus latest observed ONT signal (`OntSignalObservation`). Read-only
  projection in `app.services.field.fiber`.
- **F2** — `GET /field/fiber/splice-proposals`: the technician's own splice
  change requests with review status and notes. Vendors have the mirrored
  `GET /vendor/fiber/splices`.
- **F3** — completion gate in `app.services.field.transitions`: a work order
  that declares `requires_as_built_evidence` **and** belongs to a native
  project cannot complete without a fiber test, a topology source
  observation, or a linked splice proposal (`FieldFiberAsBuiltEvidence`).
- **F4** — vendor portal `POST /vendor/fiber/splices` (AS_BUILT_WRITE) via the
  shared intake owner `app.services.network.fiber_splice_proposals`
  (typed actors; `requested_by_vendor_id` recorded). Vendor project
  verification additionally blocks while the vendor's splice proposals are
  unreviewed (`vendor_portal_operations._verification_evidence_policy`).
- **F5** — `app.services.network.fiber_color_code` derives EIA/TIA-598 tube
  and core colors from declared cable construction (`FiberSegment.fiber_count`,
  `fibers_per_tube`, `color_standard`; migration 448). Colors are snapshotted
  into splice change-request payloads and surfaced on receipts and listings.
  Colors are derived, never stored per strand; unknown construction derives
  nothing.
- **F6** — the field customer trace annotates each traced segment with its
  reviewed strand terminations (connector port, patch panel, rack, colors)
  from the physical-continuity records.
- **F7** — `network.fiber_splice_plans` (contracted owner-command writer,
  migration 449): draft → issued → cancelled cut sheets of exact strand-end
  pairs bound to one work order (one live plan each). Admin API
  (`/fiber-splice-plans` CRUD/issue/cancel/diff), field/vendor
  `GET …/fiber/splice-plan`, execute-and-confirm on splice proposals
  (`plan_item_id` validated against the exact planned pair, plus auto-match),
  planned-vs-as-built diff (executed / pending review / unexecuted /
  unplanned), and a completion gate: a work order with an issued plan cannot
  complete until every item has an executing proposal.

- **F8** — `network.fiber_test_acceptance` (contracted POLICY owner,
  migration 450): typed per-test-type thresholds (insertion loss/OTDR event
  loss ≤ 0.30 dB, GPON class B+ receive window −28…−8 dBm, reflectance
  ≤ −35 dB; continuity/other carry an explicit `no_policy` verdict). The
  derived verdict is snapshotted beside the technician's untouched assertion
  with the policy version and applied bounds; `assertion_conflict` flags
  disagreement. A link-budget resolver derives expected downstream loss from
  the canonical trace (reviewed splitter losses + traced lengths at a named
  dB/km figure), labels incomplete traces, names every assumption, and
  surfaces margin against the latest measured ONT Rx on the field customer
  trace. Before/after baselining remains a future refinement.

- **F9** — `POST /field/fiber/ont-attachments`: the installer records the
  splitter output port chosen at the FAT as a reviewed ONT-leaf-output
  attachment proposal (owner `network.fiber_access_attachments`), scoped to
  an active ONT assignment of the job's customer. Drop-cable registration is
  covered by F10's cable registration.

- **F10** — field and vendor reviewed inventory intake
  (`app/services/network/fiber_inventory_proposals.py`, review stays with
  `network.fiber_asset_changes`): `POST …/fiber/cable-registrations`
  registers a newly built cable as an **inactive** segment change request
  with declared construction and typed provenance (activation remains with
  the reviewed connectivity flow); `POST …/fiber/strand-damage-reports`
  files reviewed damage-status updates for one exact strand or one derived
  tube. The change owner retains a reserved `provenance` payload section
  for audit without applying it to the asset.

- **F11** — resolved by derivation plus tube-scoped operations: F5 derives
  tube identity from declared construction, and F10's tube-scoped damage
  reports make that identity operational (a pinched tube marks its derived
  strands damaged in one report). **Trigger for a physical tube entity:** a
  requirement to carry state on the tube itself (tube-level slack loops,
  tube-level capacity planning, or tube splicing as a first-class record).
  Until such a requirement exists, an entity would be dead data.

- **F12** — `network.fiber_job_evidence` (contracted read-only projection):
  `GET /field/fiber/job-evidence` and `GET /vendor/fiber/job-evidence`
  aggregate, for any scoped work order, fiber tests (with derived-verdict
  failures and assertion conflicts), source observations, splice proposals
  by review status, unplanned splices, live cut-sheet progress,
  attachments, pending inventory proposals, and the as-built gate state.
  The staged-verification evidence map remains authoritative for its
  campaign.

## Open gaps

None. The technician journey gaps F1–F12 are closed; remaining refinements
are tracked inline above (before/after test baselining under F8, the tube
entity trigger under F11, and admin web UI for authoring cut sheets under
F7's API-first note).
