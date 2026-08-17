from pathlib import Path

from app.services.sot_registry.registry import service_relationship

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _source(relative: str) -> str:
    return (PROJECT_ROOT / relative).read_text(encoding="utf-8")


def test_ncc_weekly_delivery_and_report_queries_have_complete_contracts():
    delivery = service_relationship("communications.ncc_weekly_delivery")
    reporting = service_relationship("compliance.ncc_complaints_reporting")

    assert delivery.contract is not None
    assert reporting.contract is not None
    assert delivery.contract.transaction.mode.value == "owner_managed"
    assert reporting.contract.transaction.mode.value == "read_only"


def test_ncc_report_scope_uses_typed_support_provenance_and_identity_projection():
    report = _source("app/services/ncc_complaints_report.py")
    internal_source = _source("app/services/unmatched_radio_queue.py")

    assert "InternalOperationalTicketSource" in report
    assert "NccComplaintAudience" in report
    assert '"unmatched_radio_queue"' not in report
    assert '"opened_by": internal_source.value' in internal_source
    assert "source=internal_source" in internal_source
    assert "normalize_phone_identifier" in report
    assert "NccMsisdnProjection" in report


def test_ncc_adapters_delegate_transaction_and_schedule_decisions():
    task = _source("app/tasks/reports.py")
    route = _source("app/web/admin/reports.py")
    owner = _source("app/services/ncc_report_email.py")
    route_adapter = route.split("def reports_ncc_email_settings(", 1)[1].split(
        '@router.get(\n    "/ncc-weekly-runs', 1
    )[0]

    assert "execute_owner_command(" in owner
    assert "execute_owner_savepoint(" in owner
    assert "db.commit(" not in task
    assert "db.commit(" not in route_adapter
    assert "run_scheduled_ncc_report_email(db=session)" in task
    assert "send_day=send_day" in route


def test_scheduler_only_polls_and_owner_holds_tuesday_default():
    scheduler = _source("app/services/scheduler_config.py")
    owner = _source("app/services/ncc_report_email.py")

    assert 'schedule["ncc_report_email"]' in scheduler
    assert '"schedule": timedelta(minutes=5)' in scheduler
    assert 'DEFAULT_SEND_DAY = "tuesday"' in owner
    assert "Monday 08:00" not in scheduler


def test_old_best_effort_marker_and_direct_send_path_are_retired():
    owner = _source("app/services/ncc_report_email.py")
    settings = _source("app/services/settings_spec.py")

    assert "ncc_report_email_last_sent_local_date" not in owner
    assert "ncc_report_email_last_sent_local_date" not in settings
    assert "send_email(" not in owner


def test_occurrence_schema_enforces_unique_evidence_and_retires_old_marker():
    model = _source("app/models/ncc_reporting.py")
    migration = _source("alembic/versions/533_ncc_weekly_report_delivery.py")

    assert "uq_ncc_weekly_report_runs_occurrence" in model
    assert "ck_ncc_weekly_runs_state_evidence" in model
    assert "uq_ncc_weekly_report_runs_occurrence" in migration
    assert "ncc_report_email_last_sent_local_date" in migration


def test_admin_ui_exposes_complete_configuration_and_run_evidence():
    template = _source("templates/admin/reports/ncc_complaints.html")

    for field in (
        'name="recipient"',
        'name="cc"',
        'name="bcc"',
        'name="sender_key"',
        'name="subject"',
        'name="body_template"',
        'name="send_day"',
        'name="local_time"',
        'name="timezone"',
        'name="lookback_days"',
    ):
        assert field in template
    assert "Recent scheduled delivery evidence" in template
    assert "Monday 08:00" not in template


def test_crm_configuration_import_is_dry_run_first_and_uses_typed_owner():
    migration_script = _source("scripts/migration/migrate_ncc_weekly_report_config.py")

    assert '"--apply"' in migration_script
    assert "preview_configuration(command)" in migration_script
    assert "update_configuration(db=db, command=command)" in migration_script
    assert '"primary_recipient_configured"' in migration_script
    assert '"body_template_sha256"' in migration_script
