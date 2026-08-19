"""The Sub Thin Shadow cohort: 25 independently versioned owners, honestly stated.

Every entry here is `source_only` with `authority_mode = none`, and that is not a
placeholder waiting to be filled in — it is the accurate reading of the evidence:

* No cohort package is published to an index, so none has a digest-pinned
  release identity, so none can be installed anywhere.
* The shadow application runs the **pinned Sub baseline image**
  (`ghcr.io/michaelayoade/dotmac_sub@sha256:342a9b80…`, built from revision
  `9a5db5de…`, run 32216213910). That image contains none of these packages, and
  the shadow stack mounts no host paths, so there is no mechanism by which a
  cohort module could be executing in the shadow environment today.

Recording anything higher would be the exact failure this manifest exists to
prevent. The states move when the evidence moves, and `ModuleEntry` refuses each
step that runs ahead of it.

Source pins
-----------
Two revisions, because the cohort is assembled from two places. Several modules
share each pin: they are extracted from one monorepo snapshot, and inventing a
distinct revision per module to satisfy a naive uniqueness rule would make the
record less true, not more.
"""

from __future__ import annotations

from typing import Final

from app.shadow.manifest import (
    BlockingPrerequisite,
    CohortManifest,
    ComparisonGate,
    DisplacedWriter,
    ModuleEntry,
    RetirementRatchet,
)
from app.shadow.vocabulary import AdoptionState, AuthorityMode, PersistencePlane

#: Integrated Starter cohort worktree (`agent/dotmac-billing`), kernel 0.1.0a75.
COHORT_REVISION: Final[str] = "516c5a99d7331f1f3ca4e6da0b7305c9620b5733"

#: Network module suite snapshot (`agent/network-module-suite-snapshot`).
#: Built against kernel 0.1.0a73 and unvalidated — see the blocking prerequisite
#: every network entry carries.
NETWORK_SNAPSHOT_REVISION: Final[str] = "87ccc54dde2977e59d5403cda65da8d118b611e3"

_VENDOR_CP = BlockingPrerequisite(
    code="vendor-cp-platform-adoption",
    statement=(
        "Vendor CP must adopt the platform plane before this module may take "
        "production authority. The shadow environment does not satisfy this."
    ),
)
_NETWORK_RECONCILIATION = BlockingPrerequisite(
    code="network-suite-reconciliation",
    statement=(
        "Snapshot builds against kernel 0.1.0a73; the integrated cohort is at "
        "0.1.0a75. Namespace, version and lockfile changes must be reconciled "
        "and the suite validated before release."
    ),
)


def _writer(name: str, remaining: int = 1) -> DisplacedWriter:
    """A Sub SOT service this module would take over from — still standing.

    `remaining == ceiling` on purpose: nothing has been retired, and the ratchet
    must fail if a writer appears *or* disappears without review.
    """
    return DisplacedWriter(
        sub_writer=name,
        ratchet=RetirementRatchet(remaining=remaining, ceiling=remaining),
    )


def _module(
    *,
    module: str,
    package: str,
    version: str,
    revision: str,
    plane: PersistencePlane,
    gate: str,
    rollback: str,
    writers: tuple[DisplacedWriter, ...] = (),
    blocked: BlockingPrerequisite | None = None,
) -> ModuleEntry:
    """A cohort entry at its honest floor: source present, nothing claimed."""
    return ModuleEntry(
        module=module,
        package=package,
        contract_version=version,
        source_revision=revision,
        persistence_plane=plane,
        adoption_state=AdoptionState.SOURCE_ONLY,
        authority_mode=AuthorityMode.NONE,
        release=None,
        blocking_prerequisite=blocked,
        comparison_gate=ComparisonGate(
            statement=gate, reconciliation_hash=None, satisfied=False
        ),
        rollback_condition=rollback,
        displaced_writers=writers,
    )


_STARTER = COHORT_REVISION
_NET = NETWORK_SNAPSHOT_REVISION

_ENTRIES: Final[tuple[ModuleEntry, ...]] = (
    # ── Stage 1: kernel prerequisites and timers ────────────────────────────
    _module(
        module="durable-timers",
        package="dotmac-durable-timers",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.DUAL,
        gate="every shadow timer fires exactly once at the same logical instant as runtime.durable_timers",
        rollback="stop scheduling shadow timers; Sub's own scheduler is untouched",
        writers=(_writer("runtime.durable_timers"),),
    ),
    # ── Stage 2: sales → orders → subscriptions → billing → collections ─────
    _module(
        module="sales",
        package="dotmac-sales",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow lead and capture records reconcile with sales.capture for the fixture window",
        rollback="stop writing shadow leads; sales.capture remains the only writer",
        writers=(_writer("sales.capture"), _writer("sales.lead_intake")),
    ),
    _module(
        module="orders",
        package="dotmac-orders",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow order lifecycle transitions match operations.service_order_lifecycle",
        rollback="stop projecting shadow orders; the Sub order lifecycle is unchanged",
        writers=(_writer("operations.service_order_lifecycle"),),
    ),
    _module(
        module="subscriptions",
        package="dotmac-subscriptions",
        version="0.1.0a2",
        revision=_STARTER,
        plane=PersistencePlane.DUAL,
        gate="shadow subscription state equals access.subscription_lifecycle at each watermark",
        rollback="revert the coupled shadow watermark; Sub subscription authority never moved",
        writers=(_writer("access.subscription_lifecycle"),),
        blocked=_VENDOR_CP,
    ),
    _module(
        module="billing",
        package="dotmac-billing",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.DUAL,
        gate=(
            "shadow invoice, settlement and allocation output reconciles hash-for-hash "
            "with financial.invoices and financial.ledger under one coupled watermark"
        ),
        rollback="drop the coupled shadow watermark; Sub invoicing stays authoritative",
        writers=(_writer("financial.invoices"), _writer("financial.ledger")),
        blocked=_VENDOR_CP,
    ),
    _module(
        module="collections",
        package="dotmac-collections",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow dunning decisions match collections.lifecycle and financial.dunning",
        rollback="stop evaluating shadow dunning; no external consequence was ever delivered",
        writers=(_writer("collections.lifecycle"), _writer("financial.dunning")),
    ),
    # ── Stage 4: projects → work orders → surveys ───────────────────────────
    _module(
        module="projects",
        package="dotmac-projects",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow project records reconcile with the Sub work programme for the fixture window",
        rollback="drop the shadow project tables; Sub has no project owner to restore",
        # Deliberately empty: Sub's SOT registry declares no project owner, so
        # this module displaces nothing. An invented writer here would be worse
        # than an honest absence.
    ),
    _module(
        module="work-orders",
        package="dotmac-work-orders",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow work-order status transitions match operations.work_orders",
        rollback="stop projecting shadow work orders; field dispatch is untouched",
        writers=(_writer("operations.work_orders"),),
    ),
    _module(
        module="surveys",
        package="dotmac-surveys",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow survey responses reconcile with communications.surveys",
        rollback="stop recording shadow responses; no survey is ever delivered from shadow",
        writers=(_writer("communications.surveys"),),
    ),
    # ── Stage 5: inbox → campaigns ──────────────────────────────────────────
    _module(
        module="inbox",
        package="dotmac-inbox",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow conversation threading matches communications.team_inbox_projection",
        rollback="stop projecting shadow threads; the live team inbox is untouched",
        writers=(_writer("communications.team_inbox_projection"),),
    ),
    _module(
        module="campaigns",
        package="dotmac-campaigns",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow campaign audience selection matches communications.campaigns",
        rollback="stop evaluating shadow campaigns; delivery is disabled in shadow regardless",
        writers=(_writer("communications.campaigns"),),
    ),
    # ── Positioning: production adoption hold stands ────────────────────────
    _module(
        module="positioning",
        package="dotmac-positioning",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow geocoding results reconcile with gis.geocoding for the fixture set",
        rollback="stop resolving shadow positions; gis.geocoding is unchanged",
        writers=(_writer("gis.geocoding"),),
        blocked=BlockingPrerequisite(
            code="positioning-production-adoption-hold",
            statement=(
                "Positioning's production adoption hold remains in force. Shadow "
                "exercise does not lift it and does not count toward lifting it."
            ),
        ),
    ),
    # ── Stage 7: analytics, compatibility only ──────────────────────────────
    _module(
        module="analytics",
        package="dotmac-analytics",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow aggregates are compatibility-checked only; Sub declares no analytics owner",
        rollback="drop shadow aggregate tables; nothing in Sub reads them",
        blocked=BlockingPrerequisite(
            code="erp-first-adopter",
            statement="Analytics remains ERP-first; ERP's adopter requirements are unmet.",
        ),
    ),
    _module(
        module="web-analytics",
        package="dotmac-web-analytics",
        version="0.1.0a1",
        revision=_STARTER,
        plane=PersistencePlane.TENANT,
        gate="shadow web events are compatibility-checked only against synthetic traffic",
        rollback="drop shadow event tables; no live traffic is ever ingested",
        blocked=BlockingPrerequisite(
            code="backoffice-first-adopter",
            statement="Web Analytics remains Backoffice-first; those requirements are unmet.",
        ),
    ),
    # ── Stage 6: network suite, by explicit owner boundaries ────────────────
    _module(
        module="assets",
        package="dotmac-assets",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow asset change records reconcile with network.fiber_asset_changes",
        rollback="drop shadow asset tables; network.fiber_asset_changes is unchanged",
        writers=(_writer("network.fiber_asset_changes"),),
        blocked=_NETWORK_RECONCILIATION,
    ),
    _module(
        module="fiber-plant",
        package="dotmac-fiber-plant",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow plant integrity findings match network.fiber_plant_integrity",
        rollback="drop shadow plant tables; the as-built plant projection is unchanged",
        writers=(
            _writer("network.fiber_plant_integrity"),
            _writer("network.fiber_topology"),
        ),
        blocked=_NETWORK_RECONCILIATION,
    ),
    _module(
        module="inventory",
        package="dotmac-inventory",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow stock positions reconcile with network.splitter_inventory",
        rollback="drop shadow inventory tables; no stock decision leaves shadow",
        writers=(_writer("network.splitter_inventory"),),
        blocked=_NETWORK_RECONCILIATION,
    ),
    _module(
        module="ipam",
        package="dotmac-ipam",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow address assignments match network.ip_assignment_lifecycle exactly",
        rollback="drop shadow IPAM tables; no address is ever assigned from shadow",
        writers=(
            _writer("network.ip_assignment_lifecycle"),
            _writer("network.ip_pool_utilization"),
        ),
        blocked=_NETWORK_RECONCILIATION,
    ),
    _module(
        module="network-access",
        package="dotmac-network-access",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow access-path resolution matches network.access_path",
        rollback="drop shadow access tables; no session is ever authorised from shadow",
        writers=(_writer("network.access_path"),),
        blocked=_NETWORK_RECONCILIATION,
    ),
    _module(
        module="network-assurance",
        package="dotmac-network-assurance",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow maintenance lifecycle matches network.maintenance_lifecycle",
        rollback="drop shadow assurance tables; no maintenance notice is ever sent",
        writers=(_writer("network.maintenance_lifecycle"),),
        blocked=_NETWORK_RECONCILIATION,
    ),
    _module(
        module="network-control",
        package="dotmac-network-control",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow control intent matches network.control_plane_intent without emitting it",
        rollback="drop shadow intent tables; shadow never reaches a device",
        writers=(_writer("network.control_plane_intent"),),
        blocked=_NETWORK_RECONCILIATION,
    ),
    _module(
        module="network-inventory",
        package="dotmac-network-inventory",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow device inventory reconciles with network.nas_inventory",
        rollback="drop shadow device tables; the NAS inventory is unchanged",
        writers=(
            _writer("network.nas_inventory"),
            _writer("network.monitoring_inventory"),
        ),
        blocked=_NETWORK_RECONCILIATION,
    ),
    _module(
        module="network-observability",
        package="dotmac-network-observability",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow health derivation matches network.connection_health on replayed fixtures",
        rollback="drop shadow health tables; alerting is not wired to shadow",
        writers=(_writer("network.connection_health"),),
        blocked=_NETWORK_RECONCILIATION,
    ),
    _module(
        module="network-topology",
        package="dotmac-network-topology",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow forwarding topology matches network.forwarding_topology",
        rollback="drop shadow topology tables; the live topology projection is unchanged",
        writers=(_writer("network.forwarding_topology"),),
        blocked=_NETWORK_RECONCILIATION,
    ),
    _module(
        module="pon-access",
        package="dotmac-pon-access",
        version="0.1.0a1",
        revision=_NET,
        plane=PersistencePlane.TENANT,
        gate="shadow ONT commissioning decisions match network.ont_commissioning",
        rollback="drop shadow PON tables; no ONT is ever commissioned from shadow",
        writers=(_writer("network.ont_commissioning"),),
        blocked=_NETWORK_RECONCILIATION,
    ),
)

#: The cohort. `environment="shadow"` is a `Literal`, so this manifest shape
#: cannot describe a production deployment at all.
SHADOW_COHORT: Final[CohortManifest] = CohortManifest(
    manifest_version="2026-08-19.1",
    environment="shadow",
    modules=_ENTRIES,
)

__all__ = ["COHORT_REVISION", "NETWORK_SNAPSHOT_REVISION", "SHADOW_COHORT"]
