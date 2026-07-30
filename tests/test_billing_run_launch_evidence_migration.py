from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic/versions/420_billing_run_launch_evidence.py"


def test_billing_run_evidence_is_the_single_migration_head() -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    # Single linear head: ONT commissioning intents (446) sit on social comment
    # channels (445), CRM Lead delete permission (444), device projection lifecycle
    # state (443), customer VAT exemption policy (442),
    # network-zone GeoArea binding (441), composable service teams (440), billing
    # rating
    # provenance (439) and Phase 2 verification counts (438), PON port
    # administrative state (437), billing shadow verification evidence (436),
    # access invitations (435), and the ADR 0007 billing target chain (434..430),
    # which sits on inbox conversation participants (429), and chains through
    # vendor material release and advances (428), vendor principal user type (427),
    # service-team lifecycle (426), vendor project intake evidence (425),
    # proposed-route review evidence (424), prepaid opening-funding
    # reconciliation (423), conversation handoff (422), service-extension
    # activity (421), billing-run evidence (420), and
    # customer WHT (419).
    assert script.get_heads() == ["446_ont_commissioning_intents"]
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


def test_migration_retires_the_dead_schedule_and_adds_launch_evidence() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'op.drop_table("billing_run_schedules")' in source
    assert "billing_run_schedule_config" in source
    for column in (
        "launch_kind",
        "requested_by",
        "preview_fingerprint",
        "source_run_id",
    ):
        assert f'"{column}"' in source
    assert "fk_billing_runs_source_run_id" in source
    assert "ix_billing_runs_source_run_id" in source
