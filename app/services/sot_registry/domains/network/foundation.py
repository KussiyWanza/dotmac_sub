"""network SOT declarations: foundation."""

from __future__ import annotations

from app.services.sot_manifest import (
    AuthorityInput,
    AuthorityKind,
    AuthorityMigrationState,
    ConcernContract,
    ErrorContract,
    EventContract,
    MigrationContract,
    OwnerRole,
    ProjectionContract,
    ServiceContract,
    SOTService,
    TransactionContract,
    TransactionMode,
    owner_command_boundary_error_codes,
)

SERVICES: tuple[SOTService, ...] = (
    SOTService(
        name="network.identity",
        module="app.services.network.identity",
        owns=("cross-model network links", "device/entity identity"),
        notes=(
            "Current PON identity ambiguity is scoped to active PonPort rows. "
            "Inactive rows remain historical evidence and do not compete with "
            "an active port for assignment authority."
        ),
    ),
    SOTService(
        name="network.monitoring_inventory",
        module="app.services.network_monitoring",
        owns=(
            "monitoring inventory mutations",
            "monitoring device admission lifecycle transitions",
            "monitoring metric records",
            "alert rule and alert state mutations",
        ),
        depends_on=("network.identity",),
        notes=(
            "Device admission is a transition, not a flag. Every "
            "NetworkDevice.is_active change goes through "
            "set_network_device_active, which leaves polling "
            "eligibility, decays the derived live_status cache to "
            "unknown so no unpollable row keeps asserting reachability, "
            "and keeps the device visible in inventory marked inactive. "
            "Callers that flip the flag directly get half a "
            "deactivation and freeze a stale 'up' that vetoes outage "
            "detection. Router inventory (router_management) is an "
            "authoritative INPUT to the admission of the monitoring "
            "device it links — an auto-created device has no "
            "independent existence — but it requests the transition "
            "from this owner instead of writing the flag. Reachability "
            "observations never drive inventory lifecycle in either "
            "direction. Deactivating a device that still has customers "
            "attached raises an admin-facing data-integrity alert at "
            "the transition (resolved on re-admission) — a statement "
            "about the inventory record with a known blast radius, "
            "never an outage incident and never a customer-visible "
            "surface. "
            "Inventory absence must not open a customer-facing outage: "
            "an unpolled device supports no reachability verdict, which "
            "is why deactivation classifies as unknown."
        ),
    ),
    SOTService(
        name="network.core_device_archive",
        module="app.services.core_device_archive",
        owns=("reviewed core device archive and restoration",),
        depends_on=(
            "network.identity",
            "network.monitoring_inventory",
            "network.forwarding_topology",
            "network.outage_impact",
        ),
        notes=(
            "Archives only canonical NetworkDevice core rows. Archive is a "
            "reversible retirement, never projection deletion: it withdraws "
            "monitoring admission through network.monitoring_inventory, retains "
            "the row and history, and marks the device archived for the permanent "
            "device projection. Active child devices, reviewed forwarding "
            "declarations, linked NAS/router inventories, and attached customers "
            "block confirmation. Restore clears the archive tombstone but leaves "
            "the device inactive until a separate admission decision. Every "
            "legacy core-device edit or operation asks this owner for typed "
            "mutation eligibility and fails closed while the tombstone exists."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="reviewed core device archive and restoration",
                    role=OwnerRole.APPLICATION_COORDINATOR,
                    input_names=(
                        "canonical core device identity",
                        "monitoring admission lifecycle",
                        "reviewed forwarding dependencies",
                        "customer impact projection",
                    ),
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="canonical core device identity",
                    owner="network.identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="NetworkDevice identity and archive tombstone",
                ),
                AuthorityInput(
                    name="monitoring admission lifecycle",
                    owner="network.monitoring_inventory",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source="set_network_device_active transition",
                ),
                AuthorityInput(
                    name="reviewed forwarding dependencies",
                    owner="network.forwarding_topology",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source="active ForwardingTopologyDeclaration endpoints",
                ),
                AuthorityInput(
                    name="customer impact projection",
                    owner="network.outage_impact",
                    kind=AuthorityKind.DERIVED_PROJECTION,
                    source="active subscriptions in the device failure domain",
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.COORDINATOR_MANAGED,
                boundary=(
                    "Archive and restore enter execute_owner_command once on a "
                    "transaction-free session; admission, tombstone, audit, and "
                    "event evidence commit atomically."
                ),
                locking=(
                    "The command locks the exact NetworkDevice row, then reads "
                    "dependency identities in stable primary-key order and "
                    "revalidates the preview fingerprint."
                ),
                idempotency=(
                    "Archive and restore are target-state transitions. Replaying "
                    "a command after that state is reached returns the stable "
                    "current outcome without duplicating evidence."
                ),
                retries=(
                    "Adapters may retry only after rollback. A changed device or "
                    "dependency set invalidates the preview and requires review."
                ),
            ),
            errors=ErrorContract(
                domain_codes=owner_command_boundary_error_codes(
                    "network.core_device_archive"
                )
                + (
                    "network.core_device_archive.device_not_found",
                    "network.core_device_archive.impact_unavailable",
                    "network.core_device_archive.scope_mismatch",
                    "network.core_device_archive.invalid_reason",
                    "network.core_device_archive.invalid_preview_fingerprint",
                    "network.core_device_archive.stale_preview",
                    "network.core_device_archive.dependencies_block_archive",
                    "network.core_device_archive.archived_device_read_only",
                ),
                mapping_owner=(
                    "shared HTTP adapters app.web.admin.network_core_devices, "
                    "app.web.admin.network, and app.api.domains_monitoring"
                ),
                fail_closed_on=(
                    "missing device identity",
                    "unavailable impact evidence",
                    "stale preview evidence",
                    "active device dependencies",
                    "an archive tombstone on a legacy mutation target",
                    "invalid actor scope or reason",
                ),
            ),
            events=EventContract(
                event_types=(
                    "network_device.archived",
                    "network_device.restored",
                ),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Additive payload evolution within schema version 1; breaking "
                    "changes require a new event version."
                ),
                replay=(
                    "Consumers deduplicate by event identity; the authoritative "
                    "NetworkDevice tombstone remains the current-state record."
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.core_device_archive",
            ),
            steward="network operations",
            design_refs=(
                "docs/designs/CORE_DEVICE_ARCHIVE.md",
                "docs/SOT_RELATIONSHIP_MAP.md",
                "docs/UI_INFORMATION_AND_ACTION_STANDARD.md",
            ),
            test_refs=(
                "tests/test_core_device_archive.py",
                "tests/test_device_projection_views.py",
                "tests/integration/test_core_device_archive_migration.py",
                "tests/architecture/test_core_device_archive_boundary.py",
                "tests/playwright/e2e/test_core_device_archive.py",
            ),
        ),
    ),
    SOTService(
        name="network.olt_topology_import",
        module="app.services.network.olt_topology_import",
        owns=(
            "OLT shelf/card/card-port inventory from device evidence",
            "PonPort hardware linkage",
        ),
        depends_on=("network.identity",),
        notes=(
            "The OLT is authoritative for its own physical topology, so this "
            "owner imports what the device states and never asserts a layout "
            "of its own. It is the producer behind pon_port_identity's "
            "derivation chain: that owner reads "
            "PonPort -> OltCardPort -> OltCard.slot -> OltShelf.shelf, and on "
            "production the chain existed for 23 of 502 rows because the "
            "topology had never been recorded. Ports are created only where "
            "the device declares one; inventing the rest from a board model "
            "would fabricate hardware. A PonPort is linked only when its own "
            "name is canonical, because read_name strips vendor transport "
            "prefixes and matching on a stripped name would bind a row whose "
            "name the identity owner has already refused. An existing link is "
            "never silently repointed -- disagreement between a recorded link "
            "and the device is reported for review, since a PON row bound to "
            "different hardware is evidence of something worth understanding "
            "rather than a value to overwrite. Retirement of rows the device "
            "does not back is NOT owned here: this owner adds and links, and "
            "the read-only worklist reports what a reviewed decision would "
            "act on."
        ),
        contract=ServiceContract(
            concerns=(
                ConcernContract(
                    name="OLT shelf/card/card-port inventory from device evidence",
                    role=OwnerRole.RECONCILER,
                    input_names=("archived OLT running configuration",),
                    canonical_writer="network.olt_topology_import",
                ),
                ConcernContract(
                    name="PonPort hardware linkage",
                    role=OwnerRole.RECONCILER,
                    input_names=(
                        "archived OLT running configuration",
                        "canonical PON port identity",
                    ),
                    canonical_writer="network.olt_topology_import",
                ),
            ),
            authoritative_inputs=(
                AuthorityInput(
                    name="archived OLT running configuration",
                    owner="network.identity",
                    kind=AuthorityKind.AUTHORITATIVE_RECORD,
                    source=(
                        "the newest archived running config per OLT: its "
                        "interface gpon frame/slot blocks, the port "
                        "ont-auto-find lines that enumerate the board's whole "
                        "complement, and board add for the board type"
                    ),
                ),
                AuthorityInput(
                    name="canonical PON port identity",
                    owner="network.identity",
                    kind=AuthorityKind.CONTROL_INPUT,
                    source=(
                        "read_name and materialize_identity; a row whose name "
                        "is prefixed, malformed, or contested by another row "
                        "is never linked, so the identity owner decides what "
                        "may be bound and this owner only binds it"
                    ),
                ),
            ),
            transaction=TransactionContract(
                mode=TransactionMode.PARTICIPANT,
                boundary=(
                    "The caller owns the transaction. import_topology stages "
                    "shelves, cards, ports and links without committing, so a "
                    "dry run is simply a caller that rolls back."
                ),
                locking=(
                    "None taken. The import is additive and derived from an "
                    "archived config rather than a live device, so it "
                    "serializes on nothing and races nothing."
                ),
                idempotency=(
                    "Every write is a lookup-then-create on the natural key -- "
                    "(olt, shelf_number), (shelf, slot_number), (card, "
                    "port_number) -- and an already-linked PonPort is skipped "
                    "rather than repointed, so a replay creates and links "
                    "nothing further."
                ),
                retries=(
                    "Safe to re-run. Re-running after hardware topology is "
                    "corrected repairs the affected rows, which is the "
                    "intended repair path."
                ),
            ),
            errors=ErrorContract(
                domain_codes=(
                    "network.olt_topology_import.pon_row_already_linked",
                    "network.olt_topology_import.position_contested",
                ),
                mapping_owner="scripts.network.olt_topology_import",
                fail_closed_on=(
                    "network.olt_topology_import.pon_row_already_linked",
                    "network.olt_topology_import.position_contested",
                ),
            ),
            events=EventContract(
                event_types=("olt.topology_imported",),
                schema_version=1,
                delivery_owner="events.dispatcher",
                compatibility=(
                    "Emitted only when something was actually established, so "
                    "a re-run that changes nothing stays silent. The payload "
                    "carries counts rather than the topology itself: a "
                    "consumer that needs the shape reads the inventory, which "
                    "is the record, rather than reconstructing it from an "
                    "event."
                ),
                replay=(
                    "Re-running the import is the replay mechanism and is "
                    "idempotent, so a missed event costs nothing -- the "
                    "inventory it announced is already durable."
                ),
            ),
            projections=(
                ProjectionContract(
                    name="OLT shelf/card/card-port inventory",
                    input_names=("archived OLT running configuration",),
                    writer="network.olt_topology_import",
                    freshness=(
                        "As fresh as the newest archived config for that OLT. "
                        "Board topology changes rarely, so a config weeks old "
                        "remains good evidence of frames, slots and ports."
                    ),
                    stale_behavior=(
                        "A stale config understates nothing structural but may "
                        "miss a board added since. Missing hardware is "
                        "reported as an unmatched position rather than "
                        "inferred, so staleness withholds a link instead of "
                        "inventing one."
                    ),
                    drift_signal=(
                        "A PON row whose recorded link disagrees with the "
                        "device is reported as a conflict; a device position "
                        "with no PON row is reported as unmatched."
                    ),
                    rebuild_operation=(
                        "scripts/network/olt_topology_import.py, which is "
                        "idempotent and safe to re-run against a newer config."
                    ),
                    repair_owner="network.olt_topology_import",
                ),
            ),
            migration=MigrationContract(
                state=AuthorityMigrationState.NATIVE,
                new_owner="network.olt_topology_import",
                verification=(
                    "Parse tests over the production grammar (interface gpon "
                    "as the authority for frame/slot, port ont-auto-find as "
                    "the port enumeration, ont add inheriting its block's "
                    "position), plus import tests covering idempotency, the "
                    "refusal to invent a PON row for an unmatched position, "
                    "the refusal to link a non-canonical name, a prefixed twin "
                    "not blocking its canonical row, two canonical names for "
                    "one position linking neither, and an existing link being "
                    "reported rather than repointed."
                ),
            ),
            steward="network operations",
            design_refs=("docs/SOT_RELATIONSHIP_MAP.md",),
            test_refs=("tests/test_olt_topology_import.py",),
        ),
    ),
)
