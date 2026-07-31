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
| **F8** | Test acceptance is self-asserted | Open |
| **F9** | Install last mile not captured from the field | Open |
| **F10** | Field cannot record cable/closure/damage | Open |
| **F11** | Buffer tube not a physical entity | Open (mitigated by F5 derivation) |
| **F12** | Evidence map covers only staged-verification jobs | Open |

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

## Open gaps

### F8 — Test acceptance is self-asserted

`FieldFiberTestResult.passed` is set by the technician. No per-test-type
thresholds (max splice loss, ORL), no link-budget validation against the trace
(expected loss from splitter ratios + splice count + distance vs measured dB),
no before/after baselines. `topology.splice_inference` already detects Rx
droop from telemetry; the acceptance side has no policy owner.

**Owner:** a fiber test-acceptance policy service; observations stay facts,
the policy derives pass/fail.
**Fix:** typed acceptance thresholds per test type, derived verdict recorded
beside the tech's assertion, link-budget comparison sourced from the trace.

### F9 — Install last mile not captured from the field

Field equipment flow assigns the ONT, but the splitter output port chosen at
the FAT and the drop segment are admin-API records
(`domains_network_fiber`). The hand-made physical connection most likely to be
wrong in the database is the one the installer cannot record.

**Owner:** `network.fiber_access_attachments` (propose/approve already
exists for splitter-port attachments).
**Fix:** field endpoints proposing splitter-port attachment + drop capture on
install work orders, entering the same review path.

### F10 — Field cannot record cable/closure/damage

Closures, trays, strands, segments, and access points are admin-only CRUD;
the change-request enum supports update/delete but field/vendor intake only
files create-splice. A crew hanging new cable cannot register it; a tech
finding a damaged core cannot mark the strand damaged or report a cut.

**Owner:** `fiber_change_requests` (reviewed inventory operations exist —
`_reviewed_inventory` covers segments/strands under review).
**Fix:** field/vendor proposal endpoints for new cable registration and
strand-status changes (damaged/cut evidence), reviewed like splices.

### F11 — Buffer tube is not a physical entity

F5 derives tube identity from declared construction, which covers the
field-language need. There is still no tube entity for tube-level events
(a whole tube cut/pinched, tube-level slack loops), so tube-scoped damage or
capacity planning cannot be expressed.

**Fix:** only if tube-level operations become real: a tube entity between
segment and strand, populated from construction, with strands keyed to tubes.

### F12 — Evidence map covers only staged-verification jobs

`get_work_order_evidence_map` fails closed unless the work order carries
staged-feature observations, so ordinary install/repair jobs get no "what
evidence exists for this job" view. Fiber tests, splice proposals (now
linked to work orders), and attachments are not aggregated anywhere
technician-facing.

**Owner:** `network.fiber_topology_work_order_evidence_map` for the staged
campaign; a general per-job evidence projection is a separate read-only owner.
**Fix:** job evidence summary endpoint aggregating tests, observations,
splice proposals, and attachments for any scoped work order (the completion
gate of F3 already counts them — project the same resolution).
