# Fiber Topology Source of Truth

Status: canonical topology ownership in active development
Owner: `network.fiber_topology`
Mutation owner: `network.fiber_asset_changes`
Plant-integrity owner: `network.fiber_plant_integrity`
Physical-continuity owner: `network.fiber_physical_continuity`
Splitter-inventory owner: `network.splitter_inventory`
Support-structure owner: `network.fiber_support_structures`
Identity-decision owner: `network.fiber_identity_decisions`
Review/reconciliation owner: `network.fiber_identity_review`
Field-observation owner: `network.fiber_field_observations`
Field-worklist owner: `network.fiber_field_verification_worklist`
Field-map owner: `network.fiber_field_verification_map`
Work-order evidence-map owner: `network.fiber_work_order_evidence_map`
Field-mobile evidence consumer: read-only projection of
`network.fiber_work_order_evidence_map` (no independent decision owner)
Identity-coverage owner: `network.fiber_identity_coverage`
Connectivity-decision owner: `network.fiber_connectivity_decisions`
Connectivity-review owner: `network.fiber_connectivity_review`
Connectivity-coverage owner: `network.fiber_connectivity_coverage`
Numeric cutover-readiness owner: `network.fiber_cutover_readiness`
Access-attachment owner: `network.fiber_access_attachments`
Forwarding-topology owner: `network.forwarding_topology`
Electronic-topology observation owner: `network.ont_topology_observations`
Electronic-identity repair owner: `network.ont_assignment_identity`
Cleanup-batch owner: `network.ont_assignment_cutover_batches`
Cleanup-verification owner: `network.ont_assignment_cutover_verification`
Cleanup-coverage owner: `network.ont_assignment_cutover_coverage`
Constraint-authorization owner: `network.ont_assignment_constraint_authorization`
Inventory identity-release owner: `network.ont_inventory_release`
Production system: Sub at `selfcare.dotmac.io`

## Outcome

Sub owns the operational path from OLT to customer. Map files, OLT pollers,
field observations, CRM construction routes, and monitoring systems provide
facts. They do not independently decide fiber asset identity, connectivity, or
customer impact.

The target trace is:

```text
POP -> fiber rack -> OLT connector -> patch cord -> ODF connector
    -> exact cable core/termination/splice chain -> root splitter
    -> zero or more reviewed splitter cascades -> exact drop core
    -> ONT connector -> ONT -> subscription -> customer
```

Only validated edges participate in outage impact or customer diagnosis. A
line that merely appears to touch a point on a map is not a connectivity edge.

The passive-fiber trace begins at the serving POP resolved by
`network.identity`; it does not relabel LLDP adjacency as passive connectivity.
The separate `network.forwarding_topology` owner now governs the upstream
device lane. Customer path and outage ancestry consume only its reviewed
declarations with current exact required observation agreement.

## Canonical edges

| Edge | Canonical record | Legacy/observed alternatives |
| --- | --- | --- |
| POP to OLT | active `NetworkDevice` OLT match plus `pop_site_id` | names and map proximity are matching evidence only |
| OLT to PON | `PonPort.olt_id` | board/port labels are observed identifiers |
| PON to ONT | `OntUnit.pon_port_id` | `OntAssignment.pon_port_id` is a projection that must agree |
| ONT to service | active `OntAssignment.subscription_id` | subscriber/address matches are noncanonical evidence and never create the edge |
| PON to splitter input | active `PonPortSplitterLink` to an input port | inferred PON/splitter proximity is evidence only |
| Splitter output to downstream input | active `SplitterCascadeLink` between exact directed ports | cabinet, name, ratio, geometry, and proximity are evidence only |
| Splitter output to ONT | `OntUnit.splitter_port_id` to an output port | `SplitterPortAssignment` is legacy matching evidence |
| Asset-to-asset cable path | `FiberTerminationPoint` plus `FiberSegment` endpoints | route geometry alone is not connectivity |
| Exact core continuity | active reviewed `FiberStrandTermination`, `FiberCoreSplice`, and `FiberPatchCord` links over exact `FiberConnectorPort` and `FiberStrand.segment_id` identities | `FiberSplice`, strand endpoint fields, `FiberSegment.fiber_strand_id`, names, labels, and geometry are historical/display evidence only |
| Rack and ODF position | reviewed `FiberRack`, `FiberPatchPanel`, and `FiberConnectorPort` inventory with exact host, rack-unit, and port capacity | equipment names, cabinet proximity, patch labels, and map symbols never create a rack, port, or link |
| Customer fault verdict | `network.connection_health` using access path and outage impact | UI map state and raw telemetry do not decide the verdict |
| Fiber fault candidate ranking | `network.fiber_topology.localize_fiber_fault` over validated trace assets and fresh OLT cohorts | a highlighted map asset is not a confirmed failure or incident |

Coordinates and spatial projections remain owned by `gis.spatial_sync`. Fiber
topology owns what an asset is and how it connects; GIS owns where its approved
spatial projection is stored.

The admin Assign Subscriber modal resolves subscription choices only from the
exact selected Subscriber identifier. It displays all of that Subscriber's
subscriptions with an explicit Active or Inactive/current-status indicator;
the assignment owner still revalidates the exact Subscriber/Subscription
relationship when the form is submitted.

## Exact racks, patches, and cable-core continuity

`network.fiber_physical_continuity` is the only writer for active core splices,
strand-end terminations, and patch cords. It also owns the invariants applied to
reviewed fiber-rack, ODF/patch-panel, and connector-port inventory changes.
Every connector row represents one optical channel. Duplex or other grouped
single-channel patch cords may share an `assembly_label`, but grouping never
creates unrecorded channels or connectivity. `mpo` and `mtp` inventory fails
closed: MPO/MTP fan-out remains unsupported until an explicit lane/assembly
contract can preserve every optical channel and resulting continuity edge.

An active physical link requires a write-free preview, an exact hashed proposal,
independent review, locked revalidation, and an exact hashed result. A strand end
can have only one active splice or termination; a connector can have only one
active back-side termination and one active patch; panel positions must remain
inside declared rack and port capacity. Disconnecting any link in a component
that reaches an `in_use` core fails closed until service use is removed.

Core resolution starts and ends at exact PON, splitter-port, or ONT connectors.
It traverses only reviewed terminations, patch cords, numbered cable cores, and
core splices, and must use every logical cable segment once and in order. Missing
connectors, ambiguous paths, a core not marked `in_use`, or disagreement between
a rack host and the cable endpoint is a typed gap. Names, labels, geometry,
legacy `FiberSplice` rows, and the legacy `FiberSegment.fiber_strand_id` scalar
are never promoted into a path.

Field splice proposals now name both exact strand ends and create the canonical
physical-link proposal. `network.fiber_asset_changes` remains the independent
review transport and delegates approval and execution back to the physical
owner. Direct legacy splice create, update, and delete adapters return `410`;
legacy rows remain readable as historical evidence.

## Production audit — 2026-07-17

The audit was aggregate and read-only against Sub production. No customer
identifiers or secrets were extracted.

### Electronic path

- 4,054 active subscriptions are fiber.
- 1,504 active ONTs exist; 1,457 have a PON and 1,443 have an OLT.
- 1,412 active ONT assignments exist.
- 1,284 active subscriptions have an exact `subscription_id` assignment; 1,310
  resolve only when subscriber fallback is allowed.
- 39 subscriptions/subscribers have multiple active ONT assignments.
- 14 active ONTs reference a PON owned by a different OLT.
- 19 active assignment PONs disagree with the ONT PON; 10 point outside the
  ONT's OLT.
- One active assignment references an inactive ONT.
- 7 active OLTs exist; 5 are mapped through a monitoring node to a POP.
- 685,289 signal observations cover all 1,504 active ONTs. This is sufficient
  for PON/co-failure clustering, but does not identify an unnamed passive asset.

### Passive plant and locations

- Zero FDH cabinets, splitters, splitter ports, PON/splitter links, FAT/access
  points, splice closures, trays, splices, strands, terminations, and fiber
  segments are loaded.
- All 36 active POPs lack coordinates.
- No active ONT has GPS or splitter linkage.
- No active ONT assignment has a service address.

Sub can currently localize a correlated failure to an OLT/PON/ONT cohort. It
cannot truthfully name a cabinet, FAT, closure, strand, segment, or drop as the
likely fault area.

## Map-source classification

Checked-in KMZ files are source evidence, not production truth:

| Source | Rows | Identity quality | Treatment |
| --- | ---: | --- | --- |
| OSP Paths | 1,600 | 1,600 unique `spanid` values | primary staged cable geometry |
| OSP Access point | 286 | 286 unique `access_pointid` values | primary staged FAT/FAP identity |
| OSP Cabinet | 113 | 113 unique `fibermngrid` values | primary staged cabinet identity |
| OSP Splice info | 1,021 | 1,021 unique `enclosureid` values | primary staged closure identity |
| OSP Building | 1,146 | 1,146 unique `buildingid` values | primary staged building evidence |
| OSP Air fiber | 515 | 515 unique `poleid` values | primary staged support/pole evidence |
| My Map | 19,897 | 4,843 unique names; 15,054 duplicate-name rows | corroborating evidence only |
| My Places Updated map | 16,671 | 2,168 unique names; 14,503 duplicate-name rows | corroborating evidence only |

The CRM vendor-route dataset is construction/commercial evidence. Proposed
routes must not become as-built operational topology without an approved
as-built record. As of this audit, none of those CRM route records or the KMZ
plant records are loaded into Sub production.

## Import and cleanup gates

1. Freeze direct import. The legacy KMZ command is preview-only; whole-table
   purge and direct commit are retired.
2. Stage every source row with source name, stable external ID, content hash,
   raw properties, normalized geometry, and duplicate/match candidates.
3. Produce an operator-reviewed plan: create, update, merge, reject, or hold.
   Name-only and geometry-nearness matches never auto-merge.
4. Repair electronic blockers first: exact subscription assignments, duplicate
   active assignments, ONT/PON/OLT disagreement, OLT-to-POP coverage.
5. Apply approved point-asset identities before cable edges. Preserve source
   provenance and every rejected/merged identity decision.
6. Build terminations and segments. Every operational segment requires two
   referenced termination points and approved geometry.
7. Attach PON input and customer output/drop edges. Splitter port direction
   must be valid and the PON/ONT OLT must agree.
8. Run customer traces in shadow and compare predicted cohorts with OLT signal
   co-failure clusters and field evidence.
9. Cut outage/field-service readers over only when integrity blockers are zero,
   an exhaustive trace-cohort audit meets the approved threshold, and sampled
   paths are field-verified. Aggregate inventory counts alone cannot open this
   gate.
10. Retire subscriber/address fallbacks, duplicate assignment PON state,
    `SplitterPortAssignment` customer authority, and remaining direct CRUD
    writers after their consumers use the owner.

Operational cable integrity is a hard cutover invariant, not a map warning:

- An active `FiberSegment` must have two distinct active
  `FiberTerminationPoint` rows, approved route geometry, and exact canonical
  endpoint references. It must also declare a positive `fiber_count`; the
  reviewed mutation workflow delegates to `network.fiber_plant_integrity`,
  which materializes that many exact numbered
  `FiberStrand.segment_id` rows. `cable_name` is display metadata and cannot
  establish core ownership. A name, line endpoint coordinate, nearest point,
  or imported feature ID cannot satisfy an endpoint.
- Valid cable ends terminate on actual fiber infrastructure such as a PON port,
  splitter port, FDH/cabinet, FAT/access point, splice closure, or ONT. A pole or
  support is a route mount unless a separately modeled termination asset exists
  there; mounting a cable does not terminate it.
- An active segment component must be rooted through exact edges at a serving
  PON/OLT boundary. An isolated closure-to-closure or cabinet-to-cabinet island
  remains planned/staged evidence and cannot become operational merely because
  both cable ends have names.
- Construction is root-first and retirement is leaf-first. Activating or
  retiring a segment cannot orphan another active segment, splitter branch,
  ONT, or customer-bearing path.

Migration `361_fiber_plant_operational_integrity` now enforces the active-row
shape at the database boundary and fails its preflight rather than silently
deactivating or completing legacy rows. `network.fiber_asset_changes` owns the
review and application workflow and delegates the exact endpoint, whole-component,
PON/OLT rootedness, numbered-core, and safe-retirement invariants to
`network.fiber_plant_integrity`. `network.splitter_inventory` is the one writer
for splitter identity, declared ratio/capacity, and modeled ports; its API and
admin adapters use the same capacity guards. Active splitters require an exact
`input_ports:output_ports` ratio and positive declared capacity, and active
modeled ports cannot exceed that declaration. Existing rows which fail the
migration preflight remain explicit cleanup blockers.

Normalized feature equivalence is tracked by
`(source_system, asset_type, external_id, content_hash)`, while replay of an
identical source cohort is idempotent by its manifest hash. Re-running a source
cannot delete assets absent from that source. Retirement is an explicit reviewed
action, not an import side effect.

## Implemented staging boundary

Migration `333_fiber_topology_staging` adds two evidence tables:

- `fiber_topology_source_batches` is the immutable source manifest. Identity is
  `(source_system, profile, manifest_sha256)` and it records the raw file hash,
  row counts, blockers, candidates, actor, and normalization metadata. Archive
  metadata or feature ordering cannot create a duplicate normalized batch.
- `fiber_topology_staged_features` stores each normalized source fact, stable
  external ID, source properties, GeoJSON, content/geometry hashes, lineage to a
  prior source fact, and a non-authoritative match suggestion.

`network.fiber_source_staging` is the sole writer. Its match states are:

- `new`: no prior or canonical candidate;
- `unchanged`: the same source identity has identical normalized content;
- `exact_external`: one canonical asset has the same stable external code;
- `candidate`: content changed, a normalized-name candidate exists, or the
  source has a possible duplicate name/geometry;
- `ambiguous`: more than one canonical candidate exists;
- `blocked`: the source row lacks stable identity, valid expected geometry, or
  valid Nigerian coordinates, or duplicates an external ID inside the batch.

These states are review evidence, not asset decisions. The staging service only
constructs `FiberTopologySourceBatch` and `FiberTopologyStagedFeature` rows.
Architecture tests prevent the staging module from constructing canonical asset
models or deleting rows.

Operator command:

```bash
# Read-only preview of all stable-ID OSP layers
python scripts/network/stage_fiber_topology_kmz.py --all-checked-in

# Persist source evidence only; still no canonical asset writes
python scripts/network/stage_fiber_topology_kmz.py \
  --all-checked-in --stage --actor "operator identity"
```

The checked-in six-source preview resolves all expected 4,681 rows with stable
IDs and zero structural/coordinate blockers. Duplicate names and geometries are
retained as review candidates rather than silently merged.

## Reviewed point-asset identity boundary

Migration `334_fiber_topology_identity_decisions` adds the reviewed boundary
between staged evidence and canonical topology:

- `fiber_topology_identity_decisions` binds each `create`, `link_existing`, or
  `reject` proposal to the staged feature ID and exact content hash. Only one
  proposal can be active for a feature. The proposer and reviewer must be
  different actors; a declined proposal remains as evidence and a corrected
  proposal can be submitted without deleting history.
- `fiber_topology_asset_source_links` is the canonical source-identity link.
  Its source key is `(source_system, source_asset_type, external_id)`, so one
  imported identity cannot silently point at multiple assets.
- `create` is enabled for cabinets, FAT/FAP access points, splice closures, and
  support structures. Execution creates a pending
  `network.fiber_asset_changes` request; it does not construct a canonical
  asset. Support requests delegate their approved mutation to
  `network.fiber_support_structures`. The source link is finalized only after
  that request is independently approved and has an exact asset ID.
- `link_existing` is enabled for those point types and service buildings. It
  writes identity provenance only and does not mutate the linked asset.
- Cable paths remain ineligible for point-identity decisions because reviewed
  termination connectivity owns path edges; geometry is never an edge.

Every transition records actor, reason/review evidence, timestamps, the staged
content hash, and (for creates) the exact resulting fiber change request. A
changed staged feature cannot be approved, executed, or linked under an old
decision.

Operator flow:

```bash
python scripts/network/review_fiber_topology_identity.py propose \
  --feature-id FEATURE_UUID --action create \
  --actor "proposer identity" --reason "review evidence"

python scripts/network/review_fiber_topology_identity.py approve \
  --decision-id DECISION_UUID --actor "independent reviewer" \
  --notes "identity and geometry verified"

# A reviewer can decline instead; the decision remains as audit evidence.
python scripts/network/review_fiber_topology_identity.py decline \
  --decision-id DECISION_UUID --actor "independent reviewer" \
  --notes "identity evidence does not support this action"

# Emits a pending fiber change request; it does not approve the asset mutation.
python scripts/network/review_fiber_topology_identity.py execute \
  --decision-id DECISION_UUID --actor "executor identity"

# Run after the fiber change request reaches applied or rejected.
python scripts/network/review_fiber_topology_identity.py finalize \
  --decision-id DECISION_UUID --actor "finalizer identity"
```

## Operator-scale review and reconciliation

Migration `335_fiber_identity_review_batches` adds immutable proposal batch
manifests and links every batch decision to its exact row. The review owner
provides four operational guarantees:

- the queue selects only the latest staged version of each source identity and
  shows match evidence, eligible actions, active/terminal decision state,
  canonical candidates, source-to-candidate distance, and source-link drift;
- only one active decision may exist for a source identity across all staged
  versions, and changed newer content blocks approval or execution of an older
  proposal;
- preview normalizes and validates every proposal row without writing;
- persistence commits the manifest and all delegated identity proposals in one
  transaction, or writes nothing when any row is blocked;
- a request hash makes exact replays return the original batch even after its
  decisions progress, while the resolved manifest hash binds every feature
  content hash and decision digest.

The finalization sweep is an idempotent reconciler. It observes pending,
applied, or rejected fiber change requests and delegates each projection to the
identity owner. It never approves an asset mutation. If source content changes
after a change request was emitted, an already-applied asset still receives the
exact old source provenance; the latest-source queue then exposes the content
drift for a separate reviewed update instead of orphaning the created asset.

Example batch file:

```json
{
  "reason": "Reviewed Abuja point-asset cleanup cohort",
  "items": [
    {"staged_feature_id": "FEATURE_UUID", "action": "create"},
    {
      "staged_feature_id": "FEATURE_UUID",
      "action": "link_existing",
      "target_asset_id": "CANONICAL_ASSET_UUID"
    }
  ]
}
```

Operator commands:

```bash
# Latest actionable identities with canonical candidate comparison
python scripts/network/review_fiber_topology_identity.py queue \
  --profile osp_cabinets --state actionable

# Read-only validation and stable hashes
python scripts/network/review_fiber_topology_identity.py preview-batch \
  --manifest cabinet-review.json --actor "proposer identity"

# Atomic manifest plus proposal persistence; no canonical writes
python scripts/network/review_fiber_topology_identity.py propose-batch \
  --manifest cabinet-review.json --actor "proposer identity"

# Repair source links from independently reviewed change-request outcomes
python scripts/network/review_fiber_topology_identity.py reconcile \
  --actor "reconciler identity" --limit 100
```

## Independently attested batch execution

Migration `336_fiber_identity_batch_control` makes the operator-scale review
and execution controls durable:

- `fiber_topology_identity_batch_reviews` stores one independent `approve` or
  `decline` attestation for a proposal batch. The attestation copies the batch
  manifest hash, proposer, reviewer, notes, and item count into a stable digest.
  The caller must supply the exact expected manifest hash, and the proposer
  cannot be the reviewer.
- Review is all-or-nothing. Every decision must still be `proposed`; approval
  also revalidates that every source version and link target is current before
  any decision changes state. A mixed or stale batch writes neither partial
  decisions nor an attestation. An exact replay returns the existing evidence.
- `fiber_topology_identity_execution_runs` records each bounded execution with
  the attestation and manifest hashes, actor, limit, decision-level outcomes,
  remaining approved count, immutable result payload, and result digest.
- One execution advances at most 100 already-approved decisions. Each decision
  uses a savepoint so an invalid/stale item remains approved and is recorded as
  an error without discarding valid outcomes from the same bounded run.
- A `create` outcome is only `change_requested`: it emits the exact pending
  `network.fiber_asset_changes` request. Batch execution never approves that
  canonical mutation. Link and reject outcomes remain owned transitions in
  `network.fiber_identity_decisions`.

Operator flow:

```bash
# Inspect the exact manifest and all review/execution evidence.
python scripts/network/review_fiber_topology_identity.py inspect-batch \
  --batch-id BATCH_UUID

# Independent all-or-nothing review of the exact displayed hash.
python scripts/network/review_fiber_topology_identity.py attest-batch \
  --batch-id BATCH_UUID --expected-manifest-sha256 MANIFEST_SHA256 \
  --action approve --actor "independent reviewer" \
  --notes "source identities and actions verified"

# Advance no more than 50 approved decisions and record every outcome.
python scripts/network/review_fiber_topology_identity.py execute-batch \
  --batch-id BATCH_UUID --expected-manifest-sha256 MANIFEST_SHA256 \
  --actor "executor identity" --limit 50
```

The `network.fiber_identity_review` service owns these batch-control records
and delegates decision transitions to `network.fiber_identity_decisions`. It is
not a parallel writer for canonical point assets or source links.

## Reviewed termination and segment connectivity

Migration `337_fiber_topology_connectivity_decisions` adds the first reviewed
path-to-edge lifecycle. `network.fiber_connectivity_decisions` owns the
decision; `network.fiber_asset_changes` remains the only service that applies
canonical termination and segment mutations.

- A proposal is bound to one staged `fiber_segment`, its stable external ID,
  exact content hash, explicit action, actor/reason, segment attributes, and two
  typed canonical endpoint references. Supported endpoints in this capability are
  PON ports, splitter ports, FDH cabinets, FAT/access points, splice closures,
  and ONTs.
- Geometry is retained as exact route evidence. It never chooses, snaps, or
  infers an endpoint. The proposer supplies both canonical endpoint identities,
  and a different actor must review them while the staged source and endpoint
  assets are still current.
- `fiber_topology_termination_resolutions` gives each typed endpoint reference
  one shared termination resolution. Existing active canonical terminations are
  reused; otherwise execution emits one pending termination change request.
  Paths sharing an endpoint reuse the same pending/applied resolution instead
  of creating parallel endpoint truth.
- Only after both termination requests are applied does reconciliation emit the
  pending segment request with the two exact termination IDs and the reviewed
  source geometry. Neither execution nor reconciliation approves any request.
- Applied segments receive immutable source provenance in
  `fiber_topology_segment_source_links`. A rejected termination or segment
  request closes the decision with preserved evidence. `link_existing` can bind
  a staged path to an already-operational segment only when it has two active
  referenced terminations and route geometry.
- Active termination identity is unique by `(endpoint_type, ref_id)`. The
  `fiber_access_point` endpoint type is explicit; FATs are not hidden under the
  ambiguous legacy `terminal` type.

Operator flow:

```bash
python scripts/network/review_fiber_topology_connectivity.py propose \
  --feature-id PATH_FEATURE_UUID --action create \
  --start-endpoint-type fdh --start-endpoint-ref-id CABINET_UUID \
  --end-endpoint-type fiber_access_point --end-endpoint-ref-id FAT_UUID \
  --segment-type distribution --actor "proposer identity" \
  --reason "field-verified endpoint evidence"

python scripts/network/review_fiber_topology_connectivity.py approve \
  --decision-id DECISION_UUID --actor "independent reviewer" \
  --notes "endpoint identity and route evidence verified"

# Emits missing termination requests, or a segment request when both exist.
python scripts/network/review_fiber_topology_connectivity.py execute \
  --decision-id DECISION_UUID --actor "executor identity"

# Advance applied/rejected termination and segment request outcomes.
python scripts/network/review_fiber_topology_connectivity.py reconcile \
  --actor "reconciler identity" --limit 100
```

This capability does not infer path endpoints from spatial proximity and does not
bulk-load staged OSP paths. Production cutover remains a separate gate.

The six direct API mutation adapters for termination points and segments now
return `410 Gone`; their read endpoints remain available. This removes the
highest-risk unreviewed parallel writer. Canonical mutations are applied by
`network.fiber_asset_changes`, while staged path endpoint decisions and their
provenance remain owned by `network.fiber_connectivity_decisions`.

## Reviewed PON-input, splitter-cascade, and ONT-output attachments

Migrations `338_fiber_access_attachment_decisions` and
`359_splitter_cascade_links` make
`network.fiber_access_attachments` the one writer for the active
PON-to-root-input edge, directed splitter cascade edges, and the
ONT-to-leaf-output edge.

- `pon_input` decisions bind one exact `PonPort`, its OLT, the current active
  `PonPortSplitterLink`, and an explicit active splitter input port. One input
  can serve only one active PON.
- `splitter_cascade` decisions bind an exact upstream output and downstream
  input, the one rooted PON/OLT chain, the downstream stage, and cumulative
  insertion loss. `SplitterCascadeLink` is the canonical edge. Construction is
  root-first and removal is leaf-first; a link cannot orphan active ONTs or a
  downstream cascade.
- A splitter can have only one active upstream source. One output can feed at
  most one downstream splitter, and a cascade output cannot also feed an ONT.
  Self-edges, cycles, multi-input splitter traversal, and a downstream PON root
  are rejected. Every splitter participating in a cascade requires an explicit
  `insertion_loss_db`; ratio labels never supply a loss value.
- `network.fiber_asset_changes` remains the reviewed owner for changing a
  splitter's canonical insertion-loss property. The generic splitter write
  schema does not accept this field; its read schema exposes the current value.
- `ont_output` decisions bind one exact active `OntUnit`, its authoritative PON
  and OLT, the current output, and an explicit active leaf output. The target
  must resolve through the exact reviewed splitter tree rooted at that PON, and
  one output can serve only one active ONT.
- `OntUnit.splitter_port_id` is canonical. `OntUnit.splitter_id` is updated in
  the same transaction as its denormalized parent projection. A detach clears
  both. `SplitterPortAssignment` remains legacy matching evidence and cannot
  satisfy the attachment contract.
- Preview performs no writes. Proposal binds the exact before-state and actor
  evidence. A different actor must approve it. Execution locks and revalidates
  every bound PON/OLT/port/splitter value, then records the exact resulting IDs
  and a result digest. Stale inputs close without mutation.
- Geometry, coordinates, names, cabinets, splitter ratios, proximity, and route
  touching are not accepted as attachment evidence. They can guide field
  verification only.
- Generic PON-link create/update/delete adapters now return `410 Gone` at both
  API and service boundaries. Generic ONT create/update cannot set attachment
  fields, and the ONT location form no longer writes those fields.

Operator flow:

```bash
python scripts/network/review_fiber_access_attachments.py preview \
  --attachment-type pon_input --action attach --subject-id PON_UUID \
  --splitter-port-id INPUT_PORT_UUID

python scripts/network/review_fiber_access_attachments.py propose \
  --attachment-type ont_output --action attach --subject-id ONT_UUID \
  --splitter-port-id OUTPUT_PORT_UUID --actor "proposer identity" \
  --reason "field labels and electronic PON identity verified"

python scripts/network/review_fiber_access_attachments.py propose \
  --attachment-type splitter_cascade --action attach \
  --subject-id UPSTREAM_OUTPUT_PORT_UUID \
  --splitter-port-id DOWNSTREAM_INPUT_PORT_UUID \
  --actor "proposer identity" \
  --reason "directed ports and insertion loss independently verified"

python scripts/network/review_fiber_access_attachments.py approve \
  --decision-id DECISION_UUID --actor "independent reviewer" \
  --notes "exact PON, OLT, splitter, and output verified"

python scripts/network/review_fiber_access_attachments.py execute \
  --decision-id DECISION_UUID --actor "executor identity"
```

This capability establishes the reviewed writer and invariants. It does not import
production attachments or infer them from the map. Existing exact ONT
assignment duplicates and ONT/PON/OLT disagreements are inputs to the reviewed
electronic identity repair workflow below, not attachment evidence.

## Reviewed ONT assignment electronic identity repair

Migration `339_ont_assignment_identity_decisions` makes
`network.ont_assignment_identity` the owner of exceptional repairs to the
electronic ONT-to-service edge. This is a repair control plane, not a bulk
importer or a replacement for normal explicit service provisioning.

- `canonicalize` binds one exact active primary assignment, target
  subscription, PON, and OLT. The target subscriber is derived only from that
  subscription. The proposal must enumerate the complete set of other active
  assignments sharing the primary ONT or target subscription; omitted or extra
  conflict IDs fail closed.
- `deactivate` binds one exact active assignment and records its reviewed
  release. It accepts no replacement target or inferred owner.
- Preview is read-only. Proposal stores the exact before-state and SHA-256
  digest. A different actor must review it. Execution locks and revalidates the
  same inputs, updates the assignment and ONT in one transaction, deactivates
  exactly the reviewed conflicts, and records the exact resulting rows and
  result digest. Changed inputs close stale without mutation; applied decisions
  replay idempotently.
- Subscriber-only legacy assignments are not considered a match. Customer
  name, address, service address, map proximity, registration serial matching,
  and imported F/S/P values cannot select a subscription or authorize a repair.
- Public ONT-assignment POST/PATCH/DELETE adapters now return `410 Gone`; reads
  remain. The former registration-reconciliation `apply` path is retired and
  its command is observation-only. OLT authorization/autofind may fill empty
  ONT topology, but it cannot create, reactivate, or overwrite an assignment or
  overwrite a conflicting ONT/PON/OLT edge. Empty ONT topology is filled only
  when the observed F/S/P resolves to an already-modeled exact active PON.
  Allowlisted UISP observations use the separate evidence owner below, which
  may initialize missing PON inventory from an exact parent OLT and numeric port
  but cannot merge or overwrite existing identity.
- The admin queue at `/admin/network/ont-identity-reviews` detects relationship
  disagreements without proposing a correction. An operator supplies the exact
  primary assignment, subscription, and modeled PON IDs, then reviews a
  read-only hashed preview before recording a proposal. The adapter derives the
  subscriber only from the selected subscription, the OLT only from the
  selected PON foreign key, and the complete conflict set only from active
  assignment rows. It cannot select a target from customer names, addresses,
  geometry, device labels, or imported registrations. Approval and execution
  remain separate explicit actions in the UI.

Operator flow:

Use **Network → Fiber Plant → Identity Review** for the normal semi-automated
workflow. The CLI below remains an operational fallback with the same owner and
transition rules:

```bash
python scripts/network/review_ont_assignment_identity.py preview \
  --action canonicalize --primary-assignment-id ASSIGNMENT_UUID \
  --target-subscription-id SUBSCRIPTION_UUID --target-pon-port-id PON_UUID \
  --target-olt-id OLT_UUID --duplicate-assignment-id CONFLICT_UUID

python scripts/network/review_ont_assignment_identity.py propose \
  --action canonicalize --primary-assignment-id ASSIGNMENT_UUID \
  --target-subscription-id SUBSCRIPTION_UUID --target-pon-port-id PON_UUID \
  --target-olt-id OLT_UUID --duplicate-assignment-id CONFLICT_UUID \
  --actor "proposer identity" --reason "exact electronic identity verified"

python scripts/network/review_ont_assignment_identity.py approve \
  --decision-id DECISION_UUID --actor "independent reviewer" \
  --notes "assignment, subscription, PON, OLT, and conflicts checked"

python scripts/network/review_ont_assignment_identity.py execute \
  --decision-id DECISION_UUID --actor "executor identity"
```

The known production conflicts are not changed by this migration. No unique
active-subscription constraint is added yet because the audited production
cohort contains existing duplicates. The constraint is a later cutover gate
after reviewed repair execution and a clean exhaustive audit.

## Durable electronic-topology observation evidence

Migration `340_ont_topology_observation_evidence` makes
`network.ont_topology_observations` the only writer for external electronic
location observations that can initialize empty ONT topology. This keeps
collector facts separate from reviewed identity decisions.

- The initial allowlist contains UISP and Huawei F/S/P observations. UISP
  supplies the exact local ONT, imported parent OLT, device evidence key,
  numeric PON port, and observation time. Huawei supplies the exact local ONT,
  polled OLT, and modeled F/S/P label. Names, customer records, addresses, and
  geometry are not inputs.
- UISP reuses an exact active `(OLT, port number or deterministic port label)`
  match and may initialize a missing PON record with explicit source
  provenance. Huawei can link only an already-modeled exact active F/S/P and
  never creates inventory. Multiple, inactive, or missing matches remain
  review evidence; the owner does not choose one.
- Empty `OntUnit.olt_device_id` and `OntUnit.pon_port_id` fields may be
  initialized. Existing values are never overwritten. `OntAssignment` rows are
  never created, reactivated, or rewritten by observations.
- Every distinct source/ONT/OLT/port observation is hashed and retained with
  immutable initial result, latest canonical snapshot, first/last seen times,
  count, exact active assignment IDs, conflict IDs, and one of
  `initialized`, `confirmed`, `incomplete`, or `review_required`.
- The admin identity-review page projects unresolved `incomplete` and
  `review_required` evidence. An observation can seed an investigation with an
  exact assignment ID, but the operator must still select exact subscription
  and PON IDs, preview, propose, and obtain independent approval through
  `network.ont_assignment_identity`.
- The former inferred PON repair action is a read-only candidate audit. It
  cannot merge ports or rewrite assignment/splitter references, and assignment
  form reads cannot create PON inventory. Huawei authorization submits F/S/P
  through the observation owner instead of pre-writing the ONT, and PON
  alias/description forms require an existing active modeled port rather than
  creating or reactivating inventory from a displayed interface label.

## Legacy import, bulk migration, and inventory release cutover

This cutover removes the remaining legacy adapters that could bypass the
electronic identity owners.

- `scripts/network/import_smartolt_unconfigured.py` is now an exact,
  preview-only observation audit. It reports stable hashes, exact local
  serial/OLT/F/S/P matches, active assignment IDs, gaps, and conflicts. The
  former `--apply` and `--rollback` modes, suffix-based subscriber matching,
  ONT/assignment/credential creation, and RADIUS writes are retired. Password
  and username values are never written to its artifacts; only presence counts
  are retained.
- The provisioning migration UI no longer offers a target OLT/PON port. Forged
  or persisted jobs containing that target fail with `410 Gone`. A bulk
  subscriber selection cannot bind the exact assignment, subscription, ONT,
  PON, OLT, and conflict set required by `network.ont_assignment_identity`.
- `network.ont_inventory_release` owns the local electronic-identity consequence
  of an explicit return-to-inventory transition. After the orchestrator has
  completed external OLT/ACS cleanup, it locks the ONT and every assignment,
  closes active assignments, clears subscription/subscriber/service-address and
  PON references, and clears the ONT OLT/PON/F/S/P identity in one transaction.
  It selects no replacement identity and replay is idempotent.
- The broader inventory orchestrator continues to own device cleanup,
  compensation, IP release, service-config reset, CPE handling, and candidate
  restoration. It delegates electronic identity clearing to the narrow owner.

## Normal explicit ONT assignment command ownership

`network.ont_assignment_commands` is the only constructor for a normal
`OntAssignment` and separates service identity from observations and device
configuration caches.

- Normal assignment requires an exact local ONT, exact subscription, and exact
  active modeled PON. The subscriber is derived through the subscription bridge
  and must agree with any caller-supplied subscriber. A service address, when
  supplied, is validated against that derived subscriber.
- The owner locks the ONT, PON, OLT, and active ONT/subscription assignment
  scope. Exact replay is idempotent. A legacy blank placeholder may be claimed
  only when it carries no customer or subscription identity and its PON does
  not conflict. Every other disagreement fails closed into the independently
  reviewed `network.ont_assignment_identity` workflow.
- Normal release retains the historical row and records the exact resulting
  inactive assignment. A verified physical PON move updates the existing exact
  active assignment only after device execution succeeds; the DB-only move
  shortcut is retired. Each command stages actor/source audit evidence and the
  exact resulting ONT, assignment, subscription, subscriber, PON, and OLT IDs
  in the same transaction.
- The admin assignment form is a thin adapter and requires an exact subscription
  plus visible modeled PON. Direct identity editing routes point operators to
  reviewed repair. The field equipment API now requires `subscription_id` and
  `pon_port_id`; a work order or subscriber alone cannot select service identity.
- Management IPAM uses the IPAM allocation row and ONT desired configuration as
  its authoritative state. It may update legacy fields on an existing active
  assignment but cannot create an assignment as a config holder. OLT
  authorization likewise creates inventory/topology only, never a blank
  customer assignment.
- The scheduled UFiber MAC matcher is preview-only. It reports exact candidate
  IDs and duplicate-MAC signals for field verification, but it never creates an
  assignment or stages provisioning. Subscriber names, MACs, imported device
  registrations, addresses, and geometry are evidence, not assignment commands.
- The legacy generic CRUD adapter delegates exact creates to the command owner,
  permits non-identity configuration updates only, and returns `410 Gone` for
  direct identity updates, historical creates, and deletes.

## ONT assignment constraint cutover readiness

`network.ont_assignment_cutover` is the exhaustive read-only owner for deciding
whether the current active assignment data is eligible for future
database-constraint enforcement. This capability does not add or enable
constraints.

- The audit scans every active assignment before applying any display filter or
  limit. It verifies exact subscription/subscriber projection, ONT/PON/OLT
  existence and active state, assignment/ONT topology agreement, assignment and
  release timestamps, and duplicate active ONT or subscription relationships.
- Each blocked assignment carries the persisted assignment, subscription, ONT,
  PON, OLT, and exact related-assignment identifiers plus a stable SHA-256 over
  that evidence. It never selects a replacement subscription, PON, or ONT.
- Four gates stay visibly distinct: required active-assignment identity, one
  active assignment per ONT, one active assignment per subscription, and exact
  active network targets. The ONT-uniqueness gate verifies the invariant already
  introduced by migration `070`; the report does not imply that index is absent.
  A clean report is necessary but does not itself authorize new enforcement.
- The admin review queue is a projection of the exhaustive report and routes an
  exact primary assignment into `network.ont_assignment_identity`. It does not
  auto-propose, approve, execute, or repair a finding. The existing independent
  review and locked revalidation contract remains mandatory.
- `scripts/network/audit_ont_assignment_cutover.py` prints the complete JSON
  report, requests a read-only PostgreSQL transaction, exits non-zero while any
  gate is blocked, and exposes no apply or repair mode.
- New or strengthened subscription uniqueness, not-null, trigger, or validation
  constraints remain out of scope until reviewed production cleanup is complete,
  an exhaustive audit is clean, and the cutover is explicitly approved.

## Reviewed ONT assignment cleanup batches

`network.ont_assignment_cutover_batches` owns immutable, operator-selected
cleanup manifests. It stages reviewable identity decisions; it does not add a
second repair executor or authorize constraint cutover.

- A preview binds the complete assignment-cutover audit SHA-256 and the exact finding
  SHA-256 for every selected active assignment. Each item must state its action,
  reason, exact subscription/PON/OLT targets, and the complete conflict IDs when
  canonicalizing. Names, addresses, geometry, and imported identifiers remain
  inadmissible identity selectors.
- A proposal atomically stores the immutable batch manifest and delegates every
  item to `network.ont_assignment_identity` as a linked `proposed` decision.
  Duplicate primaries, overlapping repair scopes, active decision overlap, stale
  reports, stale findings, and incomplete conflict sets fail closed.
- A different operator approves or declines the entire exact manifest. Approval
  re-runs the exhaustive audit, verifies each finding digest, and delegates each
  state transition to the identity owner in one transaction. A stale input
  leaves every decision proposed; a decline preserves every item as declined
  evidence.
- There is deliberately no batch execution endpoint, service method, or CLI
  command. Approval does not mutate an assignment. Each approved decision must
  be executed individually by `network.ont_assignment_identity`, which locks and
  revalidates its exact inputs immediately before mutation.
- Migration `341_ont_assignment_cutover_batches` stores batch manifests, review
  attestations, and the exact batch row provenance on delegated decisions.
  `scripts/network/review_ont_assignment_cutover_batch.py` exposes only preview,
  propose, approve, decline, and inspect commands.

The operator JSON input is an array such as:

```json
[
  {
    "assignment_id": "00000000-0000-0000-0000-000000000000",
    "finding_sha256": "<64-character digest from the complete audit>",
    "action": "deactivate",
    "reason": "Verified stale duplicate after field review",
    "duplicate_assignment_ids": []
  }
]
```

Preview and proposal require the exact complete audit digest:

```bash
python scripts/network/review_ont_assignment_cutover_batch.py preview \
  --items-json /approved/local/path/cleanup-items.json \
  --expected-report-sha256 '<report-sha256>' \
  --actor '<operator>' --reason '<batch reason>'
```

## Post-execution ONT cleanup verification

`network.ont_assignment_cutover_verification` owns immutable terminal-result
and fresh-audit attestations. It verifies cleanup evidence; it does not execute
a repair or authorize constraints.

- Preview validates the exact cleanup-batch manifest and review, copies every
  delegated decision's status, decision/input hashes, terminal result payload
  and result hash, and independently recomputes each applied/closed result hash.
  Missing or mismatched result evidence fails closed.
- Pending `proposed` or `approved` decisions remain visibly distinct and block
  attestation. Terminal outcomes remain separate as applied, stale-closed,
  conflict-closed, other-closed, or declined; a declined batch never claims
  cleanup success.
- Verification reruns the complete assignment-cutover audit. The immutable evidence keeps
  fresh global report/gate readiness separate from residual findings in the
  selected batch's exact primary/duplicate assignment scope. A clean batch scope
  does not imply global constraint readiness.
- PostgreSQL verification requires one fresh `REPEATABLE READ` (or stronger)
  transaction so the batch, decisions, result payloads, residual findings, and
  exhaustive report come from one consistent database snapshot. A caller that
  already opened a weaker transaction fails closed.
- The verifier must differ from the batch proposer, independent reviewer, and
  every recorded decision executor. Attestation requires the exact previewed
  evidence SHA-256 and is idempotent for that exact evidence/actor/notes tuple.
  A later authoritative-state or global-audit change creates a new evidence
  digest and may be attested as another immutable snapshot.
- Migration `342_ont_assignment_cutover_verification` stores the evidence copy,
  result/report hashes, outcome counts, residual/global readiness, verifier, and
  attestation hash. The admin queue projects the latest attestation without
  offering mutation controls.
- `scripts/network/verify_ont_assignment_cutover_batch.py` exposes only preview,
  attest, and inspect. It has no execute, apply, repair, or constraint mode.

Preview first, then attest the unchanged evidence digest:

```bash
python scripts/network/verify_ont_assignment_cutover_batch.py preview \
  --batch-id '<batch-uuid>' \
  --expected-manifest-sha256 '<manifest-sha256>' \
  --actor '<independent-verifier>' --notes '<verification notes>'

python scripts/network/verify_ont_assignment_cutover_batch.py attest \
  --batch-id '<batch-uuid>' \
  --expected-manifest-sha256 '<manifest-sha256>' \
  --expected-evidence-sha256 '<preview-evidence-sha256>' \
  --actor '<independent-verifier>' --notes '<verification notes>'
```

Even an `applied_clean_scope` attestation is only cleanup evidence. Database
constraint enforcement remains a separate explicitly approved future cutover
and requires the fresh exhaustive audit itself to be globally clean.

## ONT cleanup lineage coverage reconciliation

`network.ont_assignment_cutover_coverage` is the read-only owner of current
cleanup coverage and verification-drift projection. It consumes the exhaustive
assignment audit and every cleanup-batch and verification lineage record; it
creates no new authority and does not add or enable a database constraint.

- One PostgreSQL `REPEATABLE READ` snapshot joins current exhaustive findings to
  every immutable batch item, review, delegated decision result, and verification
  attestation. The canonical verification decision-result snapshot/digest is reused;
  the coverage owner does not rederive execution evidence differently.
- A current finding is `exact` only when both its primary assignment ID and
  finding SHA-256 match one immutable proposal item. One repair-scope match with
  changed evidence is `superseded_evidence`; no match is `unassigned`; multiple
  equally exact or scope-only matches remain `ambiguous_overlapping_coverage`.
- Decision outcome, current repair-scope state, and verification state are
  independent dimensions. A historical applied result remains applied even if
  its scope is now residual; an attestation becomes `superseded_report` when the
  current exhaustive audit changes and `decision_drift` when current canonical
  result evidence no longer matches it.
- Conservative gates require a clean exhaustive audit, exact-once coverage for
  every current finding, no pending decisions, intact terminal result evidence,
  current verification for every applied batch, and no verification decision
  drift. Passing them means only `ready_for_constraint_authorization_review`;
  it does not grant that authorization.
- `/admin/network/ont-assignment-cutover-coverage` is a read-only projection.
  `scripts/network/audit_ont_assignment_cutover_coverage.py` requests a read-only
  repeatable PostgreSQL transaction, emits the stable report as JSON, and exits
  non-zero until all conservative gates pass. Neither interface has an execute,
  apply, repair, authorization, or constraint-enablement mode.

## ONT constraint-cutover authorization evidence

`network.ont_assignment_constraint_authorization` owns immutable authorization
requests and independent approve/decline attestations.
It records evidence for a later separately reviewed database change; it cannot
create, validate, enable, disable, or remove a constraint.

- A request requires the exact current cleanup-coverage SHA-256 and
  assignment-cutover audit SHA-256 while every conservative coverage gate is clean. It
  stores the complete coverage payload, an explicitly named target environment,
  requester, reason, and explicit caller-chosen expiry. No environment is
  inferred and this owner invents no unapproved maximum validity threshold.
- Request confirmation requires the exact previewed request SHA-256. A changed
  coverage/audit snapshot or expired timestamp fails before any row is written.
  Exact replays are idempotent.
- A different actor independently approves or declines the immutable request.
  Approval re-runs cleanup coverage in the same repeatable snapshot and fails closed when
  the request expired, either report hash changed, any readiness gate regressed,
  or the previewed attestation digest changed. Decline remains possible so stale
  or expired requests can be explicitly rejected without claiming approval.
- Request and review rows have no mutable lifecycle status. Read projection
  derives awaiting-review, pending-stale, pending-expired, declined,
  approved-current, approved-stale, approved-expired, or invalid-evidence state.
  An approval loses current applicability immediately when evidence or expiry
  changes while its immutable historical attestation remains intact.
- Migration `343_ont_assignment_constraint_authorization` adds only the request
  and review evidence tables and their indexes. It does not alter assignments or
  create an assignment constraint.
- `/admin/network/ont-assignment-constraint-authorizations` is a read-only
  current-applicability projection. The CLI exposes request-preview, request,
  review-preview, review, and inspect only. Even `approved_current_evidence`
  means eligibility for a separate DDL change review, not authority to run DDL.

## Operator-scale reviewed connectivity batches

`network.fiber_connectivity_review` owns immutable operator-scale proposal
manifests, independent batch attestations, and bounded execution/reconciliation
evidence. It scales the single-path decision owner to the staged OSP paths
without introducing a bulk-import writer.

- A proposal batch contains at most 500 rows. Every row binds one exact staged
  feature ID and content SHA-256. `create` and `link_existing` rows must also
  name all four typed endpoint fields explicitly; `link_existing` endpoints must
  exactly match the named canonical segment. Geometry remains route evidence and
  is never read as an endpoint selector.
- Preview resolves and hashes the exact source, endpoint, action, segment
  attributes, actor, and reason without writing. Duplicate staged features,
  stale source content, active/terminal decisions, missing endpoints, or changed
  canonical targets block the complete proposal. Confirmation writes the
  immutable manifest and individual proposals atomically.
- One actor proposes the batch and a different actor approves or declines its
  exact displayed manifest SHA-256. Review revalidates every source and endpoint
  before transitioning all decisions; any failure writes no review and changes
  no decision state.
- Execution and reconciliation are separately bounded to 100 decisions per run
  and record exact per-decision outcomes plus remaining actionable counts.
  Execution delegates only approved decisions; reconciliation delegates only
  endpoint/segment-requested decisions. Neither mode approves a termination or
  segment request.
- `network.fiber_connectivity_decisions` remains the state-transition owner and
  `network.fiber_asset_changes` remains the canonical termination/segment
  mutation owner. The batch service constructs neither canonical assets nor
  individual decisions and cannot bypass either owner.
- Migration `344_fiber_connectivity_batch_control` adds manifest, review, and run
  evidence plus nullable batch provenance on individual decisions. Existing
  single-decision calls remain valid. The read-only admin projection is
  `/admin/network/fiber-connectivity-batches/{batch_id}`.

Operator flow:

```bash
# JSON rows require staged_feature_id, expected_feature_content_sha256, action,
# and all explicit endpoint fields for create/link_existing.
python scripts/network/review_fiber_topology_connectivity_batch.py preview \
  --manifest paths.json --actor "proposer identity" \
  --reason "field-verified path cohort"

python scripts/network/review_fiber_topology_connectivity_batch.py propose \
  --manifest paths.json --actor "proposer identity" \
  --reason "field-verified path cohort"

python scripts/network/review_fiber_topology_connectivity_batch.py approve \
  --batch-id BATCH_UUID --expected-manifest-sha256 MANIFEST_SHA256 \
  --actor "independent reviewer" --notes "sources and endpoints verified"

python scripts/network/review_fiber_topology_connectivity_batch.py execute \
  --batch-id BATCH_UUID --expected-manifest-sha256 MANIFEST_SHA256 \
  --actor "executor identity" --limit 50

python scripts/network/review_fiber_topology_connectivity_batch.py reconcile \
  --batch-id BATCH_UUID --expected-manifest-sha256 MANIFEST_SHA256 \
  --actor "reconciler identity" --limit 50
```

## Exhaustive fiber connectivity coverage evidence

`network.fiber_connectivity_coverage` is the read-only owner of the
complete latest staged-path coverage and readiness projection. It adds no schema
migration and creates no new mutation authority.

- One repeatable snapshot selects the latest `fiber_segment` fact for every
  `(source_system, asset_type, external_id)` identity across the complete staged
  cohort. There is no source profile, display limit, sample, or geometry-based
  inclusion filter in the owner. An empty cohort fails closed.
- Coverage state distinguishes blocked source, unassigned, superseded source
  evidence, overlapping applicable decisions, and exact current content.
  Lifecycle state independently distinguishes missing endpoint/batch evidence,
  pending review, declined, pending execution, pending endpoint/segment
  mutation, execution-evidence drift, applied current, provenance drift,
  explicitly reviewed rejection, stale closure, rejected mutation, and other
  failed closure.
- Exact connectivity-review manifests are rehashed and compared to every delegated batch
  row. Independent review attestations and bounded execution/reconciliation run
  payloads are rehashed and their counts checked. Advanced decisions without a
  current matching run outcome remain visible evidence drift.
- Pending termination and segment request status is projected separately from
  decision status. An applied path is current only when its active source link
  still binds the exact latest content and canonical segment, and that segment
  remains active with distinct referenced endpoints and route evidence.
- Conservative gates require a non-empty reviewable full cohort, exact-once
  current coverage, intact batch/review/run evidence, no pending review,
  execution, or canonical mutation, and every path terminally applied-current or
  explicitly reviewed-rejected. Passing means only
  `ready_for_connectivity_cutover_review`; it neither names a target environment
  nor authorizes import, execution, change-request approval, or production
  cutover.
- `/admin/network/fiber-connectivity-coverage` is a read-only projection. Its
  gates always use the complete cohort while the HTML table displays at most 250
  rows. `scripts/network/audit_fiber_connectivity_coverage.py` emits the complete
  JSON report in a read-only PostgreSQL `REPEATABLE READ` transaction and exits
  non-zero until every conservative gate passes. Neither interface has a
  proposal, review, execute, reconcile, apply, approve, or cutover mode.

```bash
python scripts/network/audit_fiber_connectivity_coverage.py

# Machine-readable complete evidence; still exhaustive and read-only.
python scripts/network/audit_fiber_connectivity_coverage.py --compact
```

## Exhaustive fiber point-identity coverage evidence

`network.fiber_identity_coverage` is the read-only owner of the
complete latest staged point-asset coverage and readiness projection. It adds
no schema migration and creates no mutation authority.

Report schema version 2 removes the obsolete reject-only support count; every
reported point type now has a canonical create/link or link-only model.

- One repeatable snapshot selects the latest source fact for every
  `(source_system, asset_type, external_id)` across cabinets, FAT/access points,
  splice closures, service buildings, and support structures. It does not
  accept a profile, sample, display limit, or geometry-based inclusion filter.
- Canonical-model state remains separate from coverage and lifecycle. Cabinets,
  FATs, closures, and support structures support reviewed create/link/reject
  decisions; buildings support link/reject. Support identity does not imply an
  equipment or cable mount.
- Exact identity batch manifests, request references, decision digests,
  independent review attestations, and bounded execution-run payloads are
  rehashed and compared with their stored columns and delegated decisions.
  Never-executed, execution-failed, advanced-without-review, stale, overlapping,
  or tampered evidence remains distinct and is not treated as terminal identity
  coverage.
- Create decisions project the passive-asset change request separately. Pending
  asset review and applied/rejected results awaiting identity reconciliation are
  distinct. The report validates the exact source/decision evidence embedded in
  the request without approving it.
- An applied identity is current only when its canonical asset exists and is
  active and its active source link still binds the exact current source system,
  profile, asset type, external ID, content hash, decision, and canonical ID.
  A rejected source identity is terminal only through an independently reviewed
  `reject` decision; a rejected asset-creation request remains unresolved.
- Conservative gates require a non-empty structurally reviewable full cohort,
  exact-once current decisions, intact batch/review/run evidence, no pending
  review, execution, asset mutation, or reconciliation, terminal current
  supported identities, including every applied or explicitly rejected support
  identity.
  Passing means only `ready_for_point_identity_cutover_review`.
- `/admin/network/fiber-identity-coverage` is GET-only. Its HTML table displays
  at most 250 rows, but every gate always uses the complete cohort.
  `scripts/network/audit_fiber_identity_coverage.py` emits the full JSON report
  inside a read-only PostgreSQL `REPEATABLE READ` transaction and exits non-zero
  while any conservative gate is blocked.

```bash
python scripts/network/audit_fiber_identity_coverage.py

# Machine-readable complete evidence; still exhaustive and read-only.
python scripts/network/audit_fiber_identity_coverage.py --compact
```

## Immutable staged fiber field-verification evidence

`network.fiber_field_observations` is the sole writer and projector
for technician observations about exact staged fiber source facts. Migration
`345_fiber_topology_field_observations` adds one append-only evidence table; it
does not add a topology, identity-decision, connectivity-decision, asset-change,
or cutover writer.

- Every observation binds an exact staged feature/content SHA-256, source
  identity/profile, native Sub work order, technician/person/system-user actor,
  observation time, client reference, explicit scope and outcome, and optional
  GPS, instrument, measurement, note, and active same-work-order private
  attachment pointers. A changed source fact must be observed again.
- Point features support explicit `identity` and `presence` observations. Fiber
  paths support `presence`, `start_endpoint`, `end_endpoint`, and
  `path_endpoints`. Canonical references must name an active approved model;
  geometry, proximity, labels, and imported candidate IDs never select one.
- Outcomes remain observations: `agrees`, `conflicts`, `not_found`,
  `inaccessible`, and `inconclusive`. Contradictory current facts are retained;
  the projection reports conflict by verification scope instead of choosing a
  winner or silently overwriting evidence.
- Claim and full-observation digests make tampering visible. A repeated client
  reference is accepted only for the exact same observation. A removed or
  wrong-job attachment, changed digest, mismatched source provenance, or newer
  source content projects as drift or superseded evidence rather than a
  topology consequence.
- Field endpoints `POST /field/fiber/source-observations` and
  `GET /field/fiber/source-observations` are thin job/technician-scoped adapters
  around the owner. They do not propose, review, approve, execute, reconcile,
  or apply topology changes.
- Connectivity coverage and identity coverage display
  `unobserved`, `superseded_only`, `current_agreement`, `current_conflict`,
  `current_inconclusive`, `conflicting_observations`, and `evidence_drift`
  separately for every latest source row. These facts are included in exact
  component-report evidence but do not alter either component owner's local
  gate. The approved numeric policy consumes them only through
  `network.fiber_cutover_readiness`; a UI or component coverage resolver cannot
  introduce a parallel threshold.

## Exhaustive fiber field-verification worklist

`network.fiber_field_verification_worklist` is the read-only owner
of the complete latest-source field-evidence worklist. It adds no migration and
no mutation authority.

- One repeatable snapshot selects the latest staged fact for every
  `(source_system, asset_type, external_id)` across cabinets, FAT/access points,
  splice closures, service buildings, supports, and fiber paths. It accepts no
  source profile, sample, or display-limit input and never hides currently
  agreeing rows from the complete cohort.
- Every row embeds the field-observation projection and binds the exact
  source batch, profile, feature/content/geometry hashes, source blockers,
  current and superseded observation counts, scope states, and existing native
  work-order references. Stable row and report SHA-256 values make changes in
  source or observation evidence explicit.
- Deterministic triage order is evidence drift, contradictory observations,
  current conflict, superseded-only evidence, unobserved, inconclusive, and
  current agreement. The accompanying next step describes evidence gathering;
  it never proposes a canonical identity, endpoint, topology mutation, or
  customer-impact consequence.
- `needs_follow_up_count` is workload inventory, not a correctness percentage or
  cutover gate. The worklist deliberately has no ready/pass/eligible field;
  only `network.fiber_cutover_readiness` applies the checked-in numeric policy to
  the complete worklist evidence.
- `/admin/network/fiber-field-verification` is GET-only. Its summary always uses
  the complete cohort while the HTML table displays the first 500 rows in
  evidence-priority order. `scripts/network/audit_fiber_field_verification.py`
  emits the complete JSON report inside a read-only PostgreSQL
  `REPEATABLE READ` transaction and does not return a cutover-readiness exit
  code.
- The worklist shows only existing work-order context and has no create, assign,
  or dispatch action. Native creation and assignment are owned by
  `operations.work_order_commands`; a separate exact-source planning owner
  delegates through it. The worklist itself remains GET-only and cannot become
  an accidental second job writer.

No source fact, observation, work order, proposal, topology decision, asset, or
cutover state is changed by this projection.

## Exact fiber field-verification map projection

`network.fiber_field_verification_map` is the read-only owner of the complete
staged-geometry overlay for the field-verification worklist. It adds no
migration and no mutation authority.

- One repeatable snapshot consumes the complete owner-produced worklist, reloads
  every referenced staged source feature, and fails closed if an identity,
  source system/profile/batch, asset/geometry type, content hash, or geometry
  hash differs. The overlay accepts no source profile, sample, display limit, or
  geographic cohort input.
- Every GeoJSON feature retains the exact staged `Point`, `LineString`, or
  `Polygon` object. Empty, malformed, or unsupported source geometry remains in
  the complete FeatureCollection with an explicit unrenderable presentation
  state; the projection never repairs, snaps, transforms, selects a nearest
  asset, or converts visual contact into a topology edge.
- Feature properties retain the worklist owner's exact source hashes, row hash,
  evidence state, priority, next evidence step, blockers, and current versus
  superseded native work-order references. A stable map-feature SHA-256 and
  complete overlay SHA-256 bind those facts to the exact staged geometry and the
  worklist report SHA-256.
- Presentation bounds are computed only from unchanged finite GeoJSON coordinate
  pairs. This is a viewport projection, not asset proximity, endpoint selection,
  route validation, or fault localization. Map color comes only from the Phase
  worklist evidence-priority field; asset types and client filters cannot redefine it.
- `/admin/network/fiber-field-verification-map` is GET-only and embeds the
  complete FeatureCollection. Browser filters change only the visible overlay;
  complete counts and hashes remain unchanged. Exact source/content/geometry,
  worklist-row, and map-feature hashes plus current/superseded job context remain
  inspectable.
- `scripts/network/audit_fiber_field_verification_map.py` emits the same complete
  JSON projection inside a read-only PostgreSQL `REPEATABLE READ` transaction.
  It has no create/assign/observe/propose/apply action and no readiness exit code.

No source geometry, source fact, observation, work order, proposal, topology
decision, asset, threshold, customer-impact state, or cutover state is changed
by this projection.

## Native work-order fiber evidence map

`network.fiber_work_order_evidence_map` is the read-only owner of an
exact field-evidence overlay for one explicitly named native Sub work order. It
adds no migration and no mutation authority.

- The owner opens the same repeatable snapshot as the field-verification map,
  resolves the exact active `work_order.id` and `public_id`, reads that job's
  immutable field observations, and consumes the complete exact-GeoJSON overlay. It
  accepts no source profile, geographic area, sample, proximity, or display
  limit input.
- Every immutable observation for the work order must appear in exactly one
  field-verification map feature's owner-produced current or superseded evidence. A missing,
  duplicated, cross-job, source-identity-mismatched, or content-state-mismatched
  observation fails the complete projection instead of being hidden or joined
  by a label, geometry, or nearest-asset guess.
- Only features backed by an immutable observation for that work order are
  returned. The response removes the complete-map evidence lists for all jobs and
  replaces them with the selected job's exact observation IDs, staged-feature
  IDs, content/claim/observation hashes, actors, scopes, outcomes, measurements,
  and attachment references. This prevents a technician from discovering
  another work order through a shared source feature.
- `current_source`, `superseded_source`, and
  `current_and_superseded_source` remain separate. The geometry is always the
  unchanged current field-verification-map source geometry. Superseded evidence retains its
  originally observed staged-feature ID and content hash and is never presented
  as verification of the current geometry or content.
- Stable observation-evidence, selected-feature, and complete report SHA-256
  values bind the job-scoped projection to the exact field-verification overlay
  and worklist hashes.
- `GET /api/v1/field/fiber/work-order-evidence-map?work_order_id=<public_id>` is
  a thin authenticated adapter. It opens the repeatable snapshot before field
  permission reads and uses the existing technician/vendor/assignment-queue
  work-order scope before delegating to the owner. The endpoint has no POST,
  create, assign, observe, propose, apply, or readiness mode.

No source geometry, observation, work order, assignment, topology decision,
asset, threshold, customer-impact state, or cutover state is changed by this
projection.

## Field-mobile work-order evidence projection

`field_mobile` is a read-only consumer of the work-order evidence-map owner. It
adds no backend migration, command, or decision authority. The local Drift
schema is version 5 only to hold bounded offline projection snapshots.

- Native job detail links to `/jobs/:id/fiber-evidence`. The repository calls
  only
  `GET /api/v1/field/fiber/work-order-evidence-map?work_order_id=<public_id>`;
  it has no create, observe, assign, schedule, propose, repair, or topology-write
  path.
- The client validates that the response work-order public ID and every feature
  work-order public ID match the requested job. Unknown evidence contexts,
  unknown geometry states, presentation/value disagreement, invalid hashes,
  invalid feature counts, or source geometry labelled exact but not renderable
  without modification fail closed.
- The map renders only the returned exact `Point`, `LineString`, and `Polygon`
  coordinates. Unsupported or invalid source geometry remains visibly listed as
  unrenderable; the client never snaps, repairs, drops, or replaces it with a
  nearest asset. An empty cohort explicitly means no immutable fiber
  observations are attached to the job and does not trigger asset discovery or
  fault-area inference.
- The work-order evidence-map owner supplies the context and geometry labels,
  semantic tones, and icon keys. Flutter maps those transport-neutral meanings to Material
  tokens; it does not maintain a competing current/superseded/unrenderable
  presentation policy. Current, superseded, combined, and unrenderable evidence
  remain distinct, and exact report, overlay, observation, content, geometry,
  and feature hashes remain inspectable.
- The offline table key is
  `(authenticated_principal, work_order_public_id, report_sha256)`: the latter
  two fields are the exact evidence identity, and principal scope prevents a
  shared device login from exposing another actor's cached job evidence. A
  successful newer response replaces older snapshots for that same principal
  and job. Network fallback reads only that principal and requested public work
  order, revalidates payload/cache hashes, and displays an explicit
  stale-until-refreshed warning. Authorization, scope, lineage-conflict, and
  other authoritative 4xx responses fail closed and never fall back to stale
  data. A snapshot is never reused across principals or jobs and is evidence
  cache only, not source or operational state.

No observation, source geometry, native work order, assignment, canonical
fiber asset, topology edge, customer-impact state, threshold, or cutover state
is changed by this projection.

## Implemented supporting ownership decisions

### Canonical support structures and reviewed mount edges

`network.fiber_support_structures` owns canonical pole/support identity,
lifecycle, ownership, inspection and lease state, plus exact passive-asset mount
edges. Migration `358_fiber_support_structures` adds the canonical support,
immutable mount-decision, and mount-edge tables.

- Imported GIS, construction, CRM, or KMZ pole rows remain staged observations.
  Reviewed point-identity create/link/reject decisions use the existing
  independent proposal and passive-asset change-request path. An approved
  support request delegates its mutation to this owner; the generic change
  service never constructs a support row.
- Support lifecycle is explicit as planned, active, suspended, or retired.
  Ownership, inspection, and lease states remain visibly separate. A support
  with active mount edges cannot be retired.
- Mount preview requires an exact active support ID and exact canonical cabinet,
  FAT/access-point, splice-closure, or fiber-segment ID. Point assets have at
  most one active support mount. A fiber segment may cross multiple supports,
  but every active edge has a unique positive route sequence.
- Attach/detach proposals bind exact support, asset, and existing-mount state
  hashes. Confirmation persists immutable proposal evidence; an independent
  actor approves or declines it; execution locks and revalidates every input,
  writes the exact edge/result hash, and stages actor audit evidence. Geometry,
  labels, external IDs, and proximity never select a mount.
- `scripts/network/review_fiber_support_mount.py` is a thin preview, propose,
  approve/decline, execute, and inspect adapter. It has no unreviewed apply mode.
  Identity coverage remains read-only and now treats support structures as a
  canonical create/link model without becoming a mount writer.

### Native work-order mutation ownership

`operations.work_order_commands` is the Sub owner for native work-order
creation/header commands, assignment decisions/projection, and assignment-queue
transitions. Dispatch API/web and field-manager adapters delegate to it;
assignment preview is read-only and execution is locked, atomic,
state-idempotent, and actor-audited with exact previous/result state. Direct
header assignment fields and field-execution status bypasses are rejected.
`operations.work_orders` remains the read owner,
`operations.field_completion` retains execution transitions, and CRM IDs remain
provenance only.

### Exact field-verification job planning

`network.fiber_field_verification_job_scope` owns the versioned exact-source
scope stored on a planned native work order, and
`network.fiber_field_verification_jobs` owns write-free preview plus confirmed
execution. A plan binds at most 100 explicitly selected current worklist rows,
every staged/row/content/geometry hash, existing job context, the complete
worklist report hash, explicit subscriber and schedule, optional technician,
and a deterministic idempotent native job ID. Execute re-runs report and plan
confirmation, then delegates create/assignment to
`operations.work_order_commands` in one transaction with audit evidence.

## Approved numeric cutover-readiness policy

`network.fiber_cutover_readiness` owns the versioned numeric policy and the
combined read-only readiness decision. Policy `fiber_topology_cutover_v1`
accepts only the complete `all_sub_operating_geographies` cohort: every latest
staged source identity and path plus every active fiber subscription. A label or
geometry filter cannot create a smaller cohort. Exact geographic membership
requires a separate authoritative membership owner before a later policy may
admit it.

The policy requires:

- 100% exact-current identity coverage and 100% current terminal
  review/result/provenance evidence;
- 100% exact-current connectivity coverage and 100% current terminal
  review/result/provenance evidence;
- zero component-owner, ambiguity, drift, pending, source, or canonical
  topology blockers;
- an exhaustive trace audit with 100% of active customer-bearing fiber paths
  complete;
- exact field-worklist membership equal to the complete identity plus path
  cohorts, with 100% current agreement for every required row; and
- authoritative field-evidence contracts for POP/OLT, feeder/trunk, cabinet,
  splitter, customer-bearing endpoint, and changed/conflicting-source scopes.

No authoritative dormant-low-risk classifier exists yet, so the database
projection classifies no row as dormant and keeps every latest staged row in the
100% required field cohort. The checked-in policy nevertheless fixes the later
audit rule: 20% of explicitly classified dormant low-risk rows, rounded up with
a minimum of 25 (or the full class when it has fewer than 25 rows). Any known
sample discrepancy blocks readiness; a discrepancy rate strictly above 2%
also expands that asset class to 100% review. Integer counts and basis points,
not floating-point percentages, decide every gate.

The current report deliberately fails closed because field observations cannot
yet authoritatively cover POP/OLT, splitter, or customer-bearing endpoint
classes. It names those missing contracts as blockers. Feeder/trunk, cabinet,
and changed/conflicting staged-source evidence is supported; no unsupported
class is inferred from labels, geometry, or asset type similarity.

`scripts/network/audit_fiber_cutover_readiness.py` runs one PostgreSQL read-only
`REPEATABLE READ` snapshot and emits the policy hash, cohort hash, component
report hashes, exact numerators/denominators, gates, blocker codes, and final
report hash. It has no profile, geography, limit, proposal, approval, apply, or
cutover command. `ready_for_cutover_review` is evidence for an independent
production change review; it does not name a target, authorize a cutover, or
mutate state. In particular, this owner cannot authorize or perform a production
cutover.

```bash
python scripts/network/audit_fiber_cutover_readiness.py

# Machine-readable complete evidence; still read-only and exhaustive.
python scripts/network/audit_fiber_cutover_readiness.py --compact
```

## Border, core, and NAS forwarding ownership

`network.forwarding_topology` is the implemented Sub owner for official
downstream-to-upstream forwarding path. The previous operational readers walked
active LLDP links and treated `NetworkDevice.role == core` as enough to derive
customer upstream chains, outage ancestry, and blast radius. That parallel
decision path is retired: `NetworkTopologyLink` is adjacency evidence and the
legacy device role remains inventory/display context only.

Each active declaration binds an exact path key, downstream device/interface/
site/role, VRF, preference, configuration owner and intent reference. Internal
and NAS paths also bind the exact upstream device/interface/site/role. Border
paths bind peer IP/ASN plus exact route prefix and next hop; NAS termination
paths bind the exact `NasDevice`, route prefix, and next hop. Device and site
roles are therefore authoritative only within this reviewed declaration graph,
not inferred from a monitoring label.

Declare and retire transitions use a write-free preview, confirmed decision
hash, independent reviewer, locked execution revalidation, audit event, and
exact hashed result. Active declarations must keep one role and site per device,
unique downstream/VRF preferences, and an acyclic graph. Configuration is not a
side effect of declaration: `network.control_plane_intent` and
`network.routeros_sot` continue to apply and verify device configuration.

The read-only reconciler keeps evidence types distinct:

- internal path: exact active LLDP endpoints;
- border peer: exact current BGP peer and routing-table observations;
- NAS termination: exact active LLDP endpoints and route observation;
- RADIUS: active-session count shown as online context, never a path gate or
  writer.

Missing, expired, conflicting, or invalid evidence fails closed. The official
graph selects the lowest-preference agreeing declaration per downstream device
and VRF. Customer path, reachability, outage localization, and affected-customer
expansion now consume that graph; no raw-observation fallback remains.

Migration authority is explicit: the old owner was direct LLDP traversal in
`app.services.topology`; the new owner is
`app.services.network.forwarding_topology`. Migration `348` adds declaration,
decision, and append-only normalized control-observation records. Before any
production application cutover, operators must load and independently review
the complete intended internal/border/NAS declaration cohort, run
`scripts/network/review_forwarding_topology.py audit`, and require current
agreement for every path needed by customer/outage operations. Deployment with
missing declarations degrades safely to no inferred ancestry rather than
restoring LLDP authority.

The normalized BGP and route observation boundary now has a production-code
RouterOS adapter in
`app.services.network.forwarding_observation_collector`. It is read-only and
declaration-scoped: active reviewed declarations select the exact devices,
peers, and prefixes to inspect; Router rows must bind exactly to
`NetworkDevice`; RouterOS local address or immediate-gateway evidence must map
to one exact `DeviceInterface`; and VRF identity must be explicit. Cached or
non-established BGP sessions, inactive routes, ambiguous mappings, fuzzy names,
and unsupported payload shapes create no fact. Accepted facts are append-only,
hashed, and expire unless refreshed.

The scheduled adapter is registered on the ingestion queue but is fail-closed
by default behind `network.forwarding_observation_collection`. Enabling that
control starts the observation shadow run only. It never declares or retires a
path, applies router configuration, or authorizes customer/outage cutover.
Production readiness still requires a complete independently reviewed
declaration cohort, fresh agreeing observations, the forwarding audit, and the
existing explicit cutover review. Until then, absent evidence continues to
produce `missing_observation` and no inferred ancestry.

`network.access_path.resolve_fiber_end_to_end_path` now forms the single
read-only subscription proof. It reverses the validated passive trace from the
customer/ONT toward the OLT, requires complete declared cable capacity and one
exact reviewed connector/patch/core/splice route, resolves exactly one OLT
`NetworkDevice`, and follows only reviewed forwarding declarations with current
observation agreement through the subscription's authoritative provisioning NAS
to a core/border root. The live RADIUS NAS is a separate observation
(`agreement`, `drift`, or `missing_observation`) and never replaces provisioning
identity. Every refused join is a typed domain gap and the full
hop/gap/declaration/fault-candidate payload has one combined SHA-256.
Production cutover still requires the documented complete reviewed declaration,
passive-inventory, observation-agreement, and field-verification cohorts; the
existence of this projection does not make empty or partial production data
ready.

## Validated subscription trace shadow

`network.fiber_topology.trace_fiber_subscription` resolves one ordered path
using only authoritative edges:

```text
serving POP -> OLT -> PON -> referenced terminations/segments
  -> FDH/FAT -> root splitter input/output
  -> zero or more exact cascade distribution paths and splitter stages
  -> leaf output -> referenced drop segments
  -> ONT -> exact subscription -> customer
```

The resolver has no subscriber, address, name, proximity, or geometry-touch
fallback. It requires exactly one active `OntAssignment.subscription_id`,
agreement between the ONT/PON/OLT records, directed splitter ports, active
referenced termination points, and active segment edges with approved geometry.
If two equally short explicit paths exist, the resolver reports an ambiguity
for manual review instead of choosing one.

The read-only admin surface is `/admin/network/fiber-trace`. Operators can find
a fiber service by customer, account, ONT serial, or subscription UUID. It shows
the validated chain, every explicit gap, telemetry freshness, shared-cohort
counts, and ranked candidate scopes. It has no topology write or outage command.

### Fiber service trace page contract

- **Screen and audience:** read-only NOC/support investigation page for tracing
  one active fiber service and deciding the next evidence-gathering step.
- **Primary entity and identifiers:** subscription/customer, account number,
  subscription UUID, offer, and ONT serial. These are internal operational data
  and remain permission-scoped by `network:fiber:read`.
- **Read owner:** `network.fiber_topology` owns the ordered validated trace,
  explicit gaps, telemetry freshness classification, and bounded fault-candidate
  ranking. The route and template do not infer topology or outage state.
- **First viewport:** search, customer/service identity, electronic and physical
  completeness, current OLT observation state, and the evidence boundary.
- **Actions:** the primary action is searching/selecting a service to trace.
  Trace links are read-only row actions; the outage-impact link is secondary.
  There are no bulk, destructive, topology-write, or outage-declaration actions,
  so no command or action-eligibility owner applies.
- **Worklist:** customer/account, offer, ONT serial/state, observation time, and
  trace link; server-side search is bounded to 20 rows (maximum 50), defaulting
  to active fiber subscriptions ordered by observed ONT state and freshness.
  There is no pagination, total, or export in this shadow surface.
- **State and provenance:** incomplete topology, stale/unavailable telemetry,
  online/offline observations, no admissible candidate, and no matching service
  stay distinct. Every gap and candidate includes backend-owned evidence or
  rationale; observations never mutate topology.
- **Depth and projection:** identity and completeness are glance depth; the
  ordered chain and ranked cohorts are investigation depth. Raw asset evidence
  is progressively disclosed in the chain. Desktop may show the chain
  horizontally; mobile keeps identity/state first and makes the chain and table
  horizontally scrollable without dropping semantics.
- **Audit and observability:** the page is read-only. Reviewed identity and
  connectivity decisions retain their own immutable provenance and audit
  records; this projection creates no official incident or outage timeline.

The aggregate audit and the combined numeric policy are deliberately separate:

- `aggregate_preconditions_ready` means inventory-level blockers are clear;
- `customer_trace_evidence_complete` additionally means an exhaustive cohort
  evaluation found every active fiber subscription trace complete;
- an omitted or limited cohort cannot establish complete trace evidence, even
  if every sampled path passes; and
- only `network.fiber_cutover_readiness` combines this evidence with the
  versioned identity, connectivity, and field-verification thresholds.

Run the exhaustive gate only as an operator audit because it resolves every
active fiber service:

```bash
python scripts/network/audit_fiber_topology.py --verify-customer-traces

# Shadow sample only; this can never report complete cohort evidence
python scripts/network/audit_fiber_topology.py \
  --verify-customer-traces --trace-limit 100
```

The local migration chain now introduces a canonical `fiber_access_point`
termination type and a unique active typed-endpoint resolution. A staged FAP/FAT
still appears in a customer trace only after the reviewed connectivity workflow
has projected its canonical termination and segment edges. The resolver never
promotes a staged point or a route touching that point into an operational hop.

## Fault localization

Fault localization is derived, never written by a map UI:

- OLT or monitoring-node outage: all downstream validated paths are candidates.
- PON co-failure: paths sharing the PON are candidates.
- Correlated optical degradation within a validated passive branch: shared
  upstream segment/closure/FAT assets are ranked candidates.
- One ONT down while peers are healthy: rank the customer drop, splitter output,
  ONT power/device, and premises path; do not declare a feeder fault.
- An incomplete trace returns the first explicit gap and the last validated
  scope. It never invents a passive asset from proximity.

The implemented ranking is bounded to one selected service and its exact active
fiber cohort on the same OLT. Stale observations are excluded. When a PON cohort
is jointly offline, the result names the PON plus its validated shared branch as
a candidate set; it does not choose an individual segment without field or
optical evidence. When peers are healthy, the customer drop/premises/ONT scope
is ranked instead. `network.outage_impact` and the outage command owner remain
the only paths that may turn validated infrastructure state into operational
consequences.

The field-service surface consumes the same trace and records observations or
reviewed change requests. It does not directly redefine topology.
