from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/408_radius_session_latest_projection.py"
VALIDATION_MIGRATION = (
    ROOT / "alembic/versions/410_validate_radius_session_latest_index.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "radius_session_latest_projection", MIGRATION
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_radius_session_projection_remains_in_the_single_migration_chain() -> None:
    module = _module()
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert module.revision == "408_radius_session_latest_projection"
    assert module.down_revision == "407_retire_parallel_radius_refresh"
    assert script.get_heads() == ["451_prepaid_sweep_cycle"]
    assert (
        script.get_revision("446_ont_commissioning_intents").down_revision
        == "445_social_comment_channels"
    )
    assert (
        script.get_revision("445_social_comment_channels").down_revision
        == "444_crm_lead_delete_permission"
    )
    assert (
        script.get_revision("444_crm_lead_delete_permission").down_revision
        == "443_device_projection_lifecycle_state"
    )
    assert (
        script.get_revision("443_device_projection_lifecycle_state").down_revision
        == "442_customer_vat_exemption_policy"
    )
    assert (
        script.get_revision("442_customer_vat_exemption_policy").down_revision
        == "441_network_zone_geo_area_binding"
    )
    assert (
        script.get_revision("441_network_zone_geo_area_binding").down_revision
        == "440_composable_service_teams"
    )
    assert (
        script.get_revision("440_composable_service_teams").down_revision
        == "439_billing_obligation_rating_provenance"
    )
    assert (
        script.get_revision("439_billing_obligation_rating_provenance").down_revision
        == "438_billing_phase2_verification_counts"
    )
    assert (
        script.get_revision("438_billing_phase2_verification_counts").down_revision
        == "437_add_pon_port_admin_enabled"
    )
    assert (
        script.get_revision("437_add_pon_port_admin_enabled").down_revision
        == "436_billing_shadow_verification_evidence"
    )
    assert (
        script.get_revision("436_billing_shadow_verification_evidence").down_revision
        == "435_access_invitations"
    )
    assert (
        script.get_revision("435_access_invitations").down_revision
        == "434_sales_funding_erp_exports"
    )
    assert (
        script.get_revision("434_sales_funding_erp_exports").down_revision
        == "433_durable_timers_collections_cases"
    )
    assert (
        script.get_revision("433_durable_timers_collections_cases").down_revision
        == "432_owner_output_receipts"
    )
    assert (
        script.get_revision("432_owner_output_receipts").down_revision
        == "431_customer_subledger_postings"
    )
    assert (
        script.get_revision("431_customer_subledger_postings").down_revision
        == "430_billing_contract_obligation_identity"
    )
    assert (
        script.get_revision("430_billing_contract_obligation_identity").down_revision
        == "429_inbox_conversation_participants"
    )
    assert (
        script.get_revision("429_inbox_conversation_participants").down_revision
        == "428_vendor_material_release_and_advances"
    )
    assert (
        script.get_revision("428_vendor_material_release_and_advances").down_revision
        == "427_vendor_principal_user_type"
    )
    assert (
        script.get_revision("427_vendor_principal_user_type").down_revision
        == "426_service_team_lifecycle"
    )
    assert (
        script.get_revision("426_service_team_lifecycle").down_revision
        == "425_vendor_project_intake_evidence"
    )
    assert (
        script.get_revision("425_vendor_project_intake_evidence").down_revision
        == "424_proposed_route_review_evidence"
    )
    assert (
        script.get_revision("424_proposed_route_review_evidence").down_revision
        == "423_prepaid_opening_funding_reconciliation"
    )
    assert (
        script.get_revision("423_prepaid_opening_funding_reconciliation").down_revision
        == "422_conversation_ticket_handoff"
    )
    assert (
        script.get_revision("422_conversation_ticket_handoff").down_revision
        == "421_service_extension_activity_sot"
    )
    assert (
        script.get_revision("421_service_extension_activity_sot").down_revision
        == "420_billing_run_launch_evidence"
    )
    assert (
        script.get_revision("420_billing_run_launch_evidence").down_revision
        == "419_customer_wht_policy_and_direct_targets"
    )
    assert (
        script.get_revision("419_customer_wht_policy_and_direct_targets").down_revision
        == "418_payment_channel_mapping_sot"
    )


def test_radius_session_projection_uses_nonblocking_postgres_ddl() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "ensure_postgres_index(bind, op.execute)" in source
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" not in source
    assert "autocommit_block()" in source


def test_forward_revision_validates_databases_already_stamped_408() -> None:
    source = VALIDATION_MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "410_validate_radius_session_latest_index"' in source
    assert 'down_revision = "409_tr069_operation_lifecycle"' in source
    assert "ensure_postgres_index(bind, op.execute)" in source
    assert "autocommit_block()" in source
