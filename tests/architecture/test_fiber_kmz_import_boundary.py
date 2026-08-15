from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IMPORTER = PROJECT_ROOT / "scripts" / "network" / "import_fiber_kmz.py"
CRM_IMPORTER = PROJECT_ROOT / "scripts" / "network" / "stage_crm_network_map.py"
CRM_SOURCE = PROJECT_ROOT / "app" / "services" / "network" / "crm_network_map_source.py"
STAGING_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "fiber_topology_staging.py"
)
IDENTITY_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "fiber_topology_identity.py"
)
REVIEW_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "fiber_topology_review.py"
)
CONNECTIVITY_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "fiber_topology_connectivity.py"
)
ACCESS_ATTACHMENT_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "fiber_access_attachments.py"
)
ONT_IDENTITY_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_assignment_identity.py"
)
ONT_IDENTITY_WEB = (
    PROJECT_ROOT / "app" / "services" / "web_network_ont_identity_reviews.py"
)
ONT_TOPOLOGY_OBSERVATION_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_topology_observations.py"
)
ONT_ASSIGNMENT_COMMAND_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_assignment_commands.py"
)
ONT_ASSIGNMENT_CUTOVER_OWNER = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_assignment_cutover.py"
)
ONT_ASSIGNMENT_CUTOVER_CLI = (
    PROJECT_ROOT / "scripts" / "network" / "audit_ont_assignment_cutover.py"
)
REGISTRATION_RECONCILER = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_topology_reconcile.py"
)
ASSIGNMENT_ALIGNMENT = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_assignment_alignment.py"
)
ONT_AUTHORIZATION = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_authorization.py"
)
OLT_WEB_TOPOLOGY = PROJECT_ROOT / "app" / "services" / "network" / "olt_web_topology.py"
ONT_ASSIGNMENT_WEB = (
    PROJECT_ROOT / "app" / "services" / "web_network_ont_assignments.py"
)
PON_INTERFACE_WEB = PROJECT_ROOT / "app" / "services" / "web_network_pon_interfaces.py"
SMARTOLT_IMPORTER = (
    PROJECT_ROOT / "scripts" / "network" / "import_smartolt_unconfigured.py"
)
PROVISIONING_MIGRATION = (
    PROJECT_ROOT / "app" / "services" / "web_provisioning_migration.py"
)
PROVISIONING_MIGRATION_TEMPLATE = (
    PROJECT_ROOT / "templates" / "admin" / "provisioning" / "migrate.html"
)
ONT_INVENTORY_RELEASE = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_inventory_release.py"
)
ONT_INVENTORY_ORCHESTRATOR = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_inventory.py"
)
ONT_ASSIGNMENT_CRUD = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_assignment_crud.py"
)
ONT_ASSIGNMENT_ADAPTER = (
    PROJECT_ROOT / "app" / "services" / "network" / "subscriber_ont_adapter.py"
)
ONT_WRITE = PROJECT_ROOT / "app" / "services" / "network" / "ont_write.py"
ONT_MANAGEMENT_IPAM = (
    PROJECT_ROOT / "app" / "services" / "network" / "ont_management_ipam.py"
)
FIELD_EQUIPMENT = PROJECT_ROOT / "app" / "services" / "field" / "equipment.py"
UFIBER_ONU_LINK = PROJECT_ROOT / "app" / "services" / "topology" / "ufiber_onu_link.py"
FIBER_API = PROJECT_ROOT / "app" / "api" / "domains_network_fiber.py"
ACCESS_API = PROJECT_ROOT / "app" / "api" / "domains_network_access.py"
SPLITTER_SERVICE = PROJECT_ROOT / "app" / "services" / "network" / "splitters.py"
ONT_SERVICE = PROJECT_ROOT / "app" / "services" / "network" / "ont_crud.py"
NETWORK_MODELS = PROJECT_ROOT / "app" / "models" / "network.py"
ACCESS_ATTACHMENT_MODELS = (
    PROJECT_ROOT / "app" / "models" / "fiber_access_attachment.py"
)


def test_legacy_kmz_importer_is_preview_only() -> None:
    source = IMPORTER.read_text(encoding="utf-8")

    assert "Direct KMZ writes are retired" in source
    assert "--purge is retired" in source
    assert ".delete(" not in source
    assert ".commit(" not in source
    assert "db.rollback()" in source


def test_crm_map_importer_can_only_delegate_staging_evidence() -> None:
    importer = CRM_IMPORTER.read_text(encoding="utf-8")
    source = CRM_SOURCE.read_text(encoding="utf-8")

    assert "stage_fiber_preview_batch(" in importer
    assert 'canonical_asset_writes": 0' in importer
    assert ".commit(" not in importer
    assert ".delete(" not in importer
    assert "SessionLocal" not in source
    assert ".commit(" not in source
    assert ".delete(" not in source
    for constructor in (
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSegment(",
        "FiberSpliceClosure(",
        "ServiceBuilding(",
        "OLTDevice(",
    ):
        assert constructor not in importer
        assert constructor not in source


def test_staging_owner_cannot_construct_or_delete_canonical_assets() -> None:
    source = STAGING_OWNER.read_text(encoding="utf-8")

    assert ".delete(" not in source
    for constructor in (
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSegment(",
        "FiberSpliceClosure(",
        "ServiceBuilding(",
    ):
        assert constructor not in source

    assert "FiberTopologySourceBatch(" in source
    assert "FiberTopologyStagedFeature(" in source


def test_identity_owner_projects_creates_through_fiber_change_requests() -> None:
    source = IDENTITY_OWNER.read_text(encoding="utf-8")

    assert ".delete(" not in source
    for constructor in (
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSegment(",
        "FiberSpliceClosure(",
        "ServiceBuilding(",
    ):
        assert constructor not in source

    assert "FiberTopologyIdentityDecision(" in source
    assert "FiberTopologyAssetSourceLink(" in source
    assert "fiber_change_requests.create_request(" in source


def test_review_owner_delegates_decisions_and_cannot_construct_assets() -> None:
    source = REVIEW_OWNER.read_text(encoding="utf-8")

    assert ".delete(" not in source
    for constructor in (
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSegment(",
        "FiberSpliceClosure(",
        "ServiceBuilding(",
        "FiberTopologyIdentityDecision(",
        "FiberTopologyAssetSourceLink(",
    ):
        assert constructor not in source

    assert "FiberTopologyIdentityProposalBatch(" in source
    assert "FiberTopologyIdentityBatchReview(" in source
    assert "FiberTopologyIdentityExecutionRun(" in source
    assert "propose_identity_decision(" in source
    assert "approve_identity_decision(" in source
    assert "decline_identity_decision(" in source
    assert "execute_identity_decision(" in source
    assert "finalize_identity_decision(" in source


def test_connectivity_owner_requires_reviewed_requests_for_canonical_edges() -> None:
    source = CONNECTIVITY_OWNER.read_text(encoding="utf-8")

    assert ".delete(" not in source
    for constructor in (
        "FdhCabinet(",
        "FiberAccessPoint(",
        "FiberSegment(",
        "FiberSpliceClosure(",
        "FiberTerminationPoint(",
    ):
        assert constructor not in source

    assert "FiberTopologyConnectivityDecision(" in source
    assert "FiberTopologyTerminationResolution(" in source
    assert "FiberTopologySegmentSourceLink(" in source
    assert "fiber_change_requests.create_request(" in source


def test_direct_api_connectivity_mutations_are_retired() -> None:
    source = FIBER_API.read_text(encoding="utf-8")

    assert "Direct termination/segment mutation is retired" in source
    assert "fiber_termination_points.create(" not in source
    assert "fiber_termination_points.update(" not in source
    assert "fiber_termination_points.delete(" not in source
    assert "fiber_segments.create(" not in source
    assert "fiber_segments.update(" not in source
    assert "fiber_segments.delete(" not in source


def test_access_attachment_owner_is_the_only_canonical_attachment_writer() -> None:
    source = ACCESS_ATTACHMENT_OWNER.read_text(encoding="utf-8")

    assert "FiberAccessAttachmentDecision(" in source
    assert "PonPortSplitterLink(" in source
    assert "SplitterCascadeLink(" in source
    assert "ont.splitter_port_id =" in source
    assert "ont.splitter_id =" in source
    for constructor in (
        "OLTDevice(",
        "OntUnit(",
        "PonPort(",
        "Splitter(",
        "SplitterPort(",
    ):
        assert constructor not in source
    for inferred_link in (
        "ST_Distance",
        "gps_latitude",
        "gps_longitude",
        "route_geom",
    ):
        assert inferred_link not in source

    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        candidate = path.read_text(encoding="utf-8")
        if path not in {
            ACCESS_ATTACHMENT_OWNER,
            ACCESS_ATTACHMENT_MODELS,
            NETWORK_MODELS,
        }:
            assert "PonPortSplitterLink(" not in candidate
            assert "SplitterCascadeLink(" not in candidate
            assert "ont.splitter_port_id =" not in candidate
            assert "ont.splitter_id =" not in candidate


def test_direct_access_attachment_mutations_are_retired() -> None:
    api_source = ACCESS_API.read_text(encoding="utf-8")
    splitter_source = SPLITTER_SERVICE.read_text(encoding="utf-8")
    ont_source = ONT_SERVICE.read_text(encoding="utf-8")

    assert "Direct PON/splitter attachment mutation is retired" in api_source
    assert "pon_port_splitter_links.create(" not in api_source
    assert "pon_port_splitter_links.update(" not in api_source
    assert "pon_port_splitter_links.delete(" not in api_source
    assert "Direct PON/splitter attachment mutation is retired" in splitter_source
    assert "Direct ONT/splitter attachment mutation is retired" in ont_source
    assert '{"splitter_id", "splitter_port_id"}' in ont_source


def test_reviewed_ont_identity_owner_binds_exact_electronic_edges() -> None:
    source = ONT_IDENTITY_OWNER.read_text(encoding="utf-8")

    assert "OntAssignmentIdentityDecision(" in source
    assert "OntAssignment.subscription_id == target_subscription_id" in source
    assert "duplicate_assignment_ids must exactly cover" in source
    assert "the proposer cannot review" in source
    assert "preview.input_sha256 != decision.input_sha256" in source
    assert "primary.subscription_id = target_subscription_id" in source
    assert "primary.subscriber_id = target_subscriber_id" in source
    assert "ont.pon_port_id = target_pon_port_id" in source
    assert "ont.olt_device_id = target_olt_id" in source
    assert "OntAssignment.subscriber_id ==" not in source
    assert ".service_address_id" not in source


def test_parallel_ont_identity_writers_are_retired() -> None:
    api_source = ACCESS_API.read_text(encoding="utf-8")
    reconcile_source = REGISTRATION_RECONCILER.read_text(encoding="utf-8")
    alignment_source = ASSIGNMENT_ALIGNMENT.read_text(encoding="utf-8")

    assert "Direct ONT assignment identity mutation is retired" in api_source
    assert "ont_assignments.create(" not in api_source
    assert "ont_assignments.update(" not in api_source
    assert "ont_assignments.delete(" not in api_source
    assert "registration-driven topology writes are retired" in reconcile_source
    assert "OntTopologyRepairReviewRequired" in reconcile_source
    assert "PonPort(" not in reconcile_source
    assert "ont.pon_port_id =" not in reconcile_source
    assert "assignment.pon_port_id =" not in reconcile_source
    assert "active_assignment.pon_port_id =" not in alignment_source
    assert "OntAssignment(" not in alignment_source
    assert "ensure_canonical_pon_port" not in alignment_source


def test_admin_ont_identity_review_is_an_explicit_thin_adapter() -> None:
    source = ONT_IDENTITY_WEB.read_text(encoding="utf-8")

    assert "preview_from_explicit_form" in source
    assert "active_assignment_identity_conflict_ids(" in source
    assert "target_olt_id=pon.olt_id" in source
    assert "propose_assignment_identity_repair(" in source
    assert "OntAssignmentIdentityDecision(" not in source
    assert ".subscriber_id =" not in source
    assert ".subscription_id =" not in source
    assert ".pon_port_id =" not in source
    for forbidden_inference in (
        "Subscriber.first_name",
        "Subscriber.last_name",
        "Address.",
        "gps_latitude",
        "gps_longitude",
        "ST_Distance",
        "OltOntRegistration",
    ):
        assert forbidden_inference not in source


def test_electronic_topology_collectors_delegate_to_one_observation_owner() -> None:
    owner_source = ONT_TOPOLOGY_OBSERVATION_OWNER.read_text(encoding="utf-8")
    alignment_source = ASSIGNMENT_ALIGNMENT.read_text(encoding="utf-8")
    authorization_source = ONT_AUTHORIZATION.read_text(encoding="utf-8")
    uisp_source = (
        PROJECT_ROOT / "app" / "services" / "topology" / "uisp_sync.py"
    ).read_text(encoding="utf-8")

    assert "OntTopologyObservationEvidence(" in owner_source
    assert "PonPort(" in owner_source
    assert "ont.olt_device_id = olt.id" in owner_source
    assert "ont.pon_port_id = pon.id" in owner_source
    assert "assignment.pon_port_id =" not in owner_source
    assert "assignment.active =" not in owner_source
    assert "observe_ont_electronic_topology(" in alignment_source
    assert "project_ont_topology_from_fsp_observation(" in authorization_source
    assert "observe_ont_electronic_topology(" in uisp_source
    assert "ont.olt_device_id =" not in alignment_source
    assert "ont.pon_port_id =" not in alignment_source
    assert "existing.olt_device_id =" not in authorization_source
    assert "olt_device_id=uuid.UUID(str(olt_id))" not in authorization_source
    assert "PonPort(" not in uisp_source


def test_inferred_pon_repair_and_assignment_read_writers_are_retired() -> None:
    topology_source = OLT_WEB_TOPOLOGY.read_text(encoding="utf-8")
    assignment_web_source = ONT_ASSIGNMENT_WEB.read_text(encoding="utf-8")
    interface_web_source = PON_INTERFACE_WEB.read_text(encoding="utf-8")

    assert "Direct inferred PON repair is retired" in topology_source
    assert "PonPort(" not in topology_source
    assert "ensure_canonical_pon_port" not in topology_source
    assert "_retire_duplicate_pon_port" not in topology_source
    assert "assignment.pon_port_id =" not in topology_source
    assert "splitter_link.pon_port_id =" not in topology_source
    assert "ensure_canonical_pon_port" not in assignment_web_source
    assert "ensure_canonical_pon_port" not in interface_web_source
    assert "PonPort(" not in interface_web_source


def test_legacy_smartolt_importer_is_exact_observation_audit_only() -> None:
    source = SMARTOLT_IMPORTER.read_text(encoding="utf-8")

    assert "Direct SmartOLT import writes are retired" in source
    assert '"mode": "preview_only"' in source
    assert "--apply" not in source
    assert "--rollback" not in source
    assert ".commit(" not in source
    assert ".delete(" not in source
    assert "OntUnit(" not in source
    assert "OntAssignment(" not in source
    assert "AccessCredential(" not in source
    assert "_resolve_subscriber" not in source
    assert "suffix5" not in source


def test_bulk_provisioning_migration_cannot_rewrite_pon_identity() -> None:
    source = PROVISIONING_MIGRATION.read_text(encoding="utf-8")
    template = PROVISIONING_MIGRATION_TEMPLATE.read_text(encoding="utf-8")

    assert "Bulk OLT/PON migration is retired" in source
    assert "assignment.pon_port_id =" not in source
    assert "_update_olt_port_for_subscriber" not in source
    assert 'name="target_pon_port_id"' not in template


def test_inventory_return_delegates_identity_release_to_one_owner() -> None:
    owner_source = ONT_INVENTORY_RELEASE.read_text(encoding="utf-8")
    orchestrator_source = ONT_INVENTORY_ORCHESTRATOR.read_text(encoding="utf-8")

    assert ".with_for_update()" in owner_source
    assert "assignment.subscription_id = None" in owner_source
    assert "assignment.subscriber_id = None" in owner_source
    assert "assignment.pon_port_id = None" in owner_source
    assert "ont.olt_device_id = None" in owner_source
    assert "ont.pon_port_id = None" in owner_source
    assert "release_ont_electronic_identity(" in orchestrator_source
    assert "assignment.subscription_id =" not in orchestrator_source
    assert "assignment.pon_port_id =" not in orchestrator_source
    assert "ont.olt_device_id =" not in orchestrator_source
    assert "ont.pon_port_id =" not in orchestrator_source


def test_normal_ont_assignment_commands_are_the_only_assignment_constructor() -> None:
    owner_source = ONT_ASSIGNMENT_COMMAND_OWNER.read_text(encoding="utf-8")

    assert "OntAssignment(" in owner_source
    assert "subscription_id: object" in owner_source
    assert "pon_port_id: object" in owner_source
    assert "resolve_assignment_subscription(" in owner_source
    assert "OntAssignment.subscription_id == sub_id" in owner_source
    assert "use reviewed identity repair" in owner_source
    assert "stage_audit_event(" in owner_source
    assert '"exact_result"' in owner_source
    for forbidden_inference in (
        "mac_address",
        "Subscriber.first_name",
        "Subscriber.last_name",
        "gps_latitude",
        "gps_longitude",
        "ST_Distance",
        "OltOntRegistration",
    ):
        assert forbidden_inference not in owner_source

    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path not in {ONT_ASSIGNMENT_COMMAND_OWNER, NETWORK_MODELS}:
            assert "OntAssignment(" not in source


def test_normal_assignment_adapters_delegate_or_fail_closed() -> None:
    web_source = ONT_ASSIGNMENT_WEB.read_text(encoding="utf-8")
    crud_source = ONT_ASSIGNMENT_CRUD.read_text(encoding="utf-8")
    adapter_source = ONT_ASSIGNMENT_ADAPTER.read_text(encoding="utf-8")
    write_source = ONT_WRITE.read_text(encoding="utf-8")
    ipam_source = ONT_MANAGEMENT_IPAM.read_text(encoding="utf-8")
    field_source = FIELD_EQUIPMENT.read_text(encoding="utf-8")
    ufiber_source = UFIBER_ONU_LINK.read_text(encoding="utf-8")
    authorization_source = ONT_AUTHORIZATION.read_text(encoding="utf-8")

    assert "ont_assignment_commands.assign(" in web_source
    assert "Exact service subscription is required" in web_source
    assert "ont_assignment_commands.assign(" in adapter_source
    assert "ont_assignment_commands.release(" in adapter_source
    assert "self._command_owner.assign(" in crud_source
    assert "Direct ONT assignment identity updates are retired" in crud_source
    assert "ont_assignment_commands.move_to_pon(" in write_source
    assert "DB-only ONT moves are retired" in write_source
    assert "_get_or_create_active_assignment" not in ipam_source
    assert "ont_assignment_commands.assign(" in field_source
    assert "ont_assignment_commands.release(" in field_source
    assert "matched_candidate" in ufiber_source
    assert "db.add(" not in ufiber_source
    assert "db.flush(" not in ufiber_source
    assert "_get_or_create_active_assignment" not in authorization_source


def test_assignment_constraint_cutover_is_exhaustive_read_only_evidence() -> None:
    owner_source = ONT_ASSIGNMENT_CUTOVER_OWNER.read_text(encoding="utf-8")
    cli_source = ONT_ASSIGNMENT_CUTOVER_CLI.read_text(encoding="utf-8")
    web_source = ONT_IDENTITY_WEB.read_text(encoding="utf-8")

    assert "audit_ont_assignment_cutover" in owner_source
    assert "OntAssignment.active.is_(True)" in owner_source
    assert ".limit(" not in owner_source
    assert "report_sha256" in owner_source
    assert 'REPAIR_OWNER = "network.ont_assignment_identity"' in owner_source
    for mutation in ("db.add(", "db.commit(", "db.flush(", "db.delete("):
        assert mutation not in owner_source

    assert "read_only_snapshot_session()" in cli_source
    assert "--apply" not in cli_source
    assert "--repair" not in cli_source
    assert "audit_ont_assignment_cutover(db)" in cli_source
    assert "audit_ont_assignment_cutover(db)" in web_source
    assert "_duplicate_ids" not in web_source
