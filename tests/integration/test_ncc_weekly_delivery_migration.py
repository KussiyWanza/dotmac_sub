from sqlalchemy import inspect, text


def test_migrated_ncc_weekly_run_schema_and_marker_retirement(db_session):
    inspector = inspect(db_session.get_bind())

    assert "ncc_weekly_report_runs" in inspector.get_table_names()
    constraints = {
        item["name"]
        for item in inspector.get_unique_constraints("ncc_weekly_report_runs")
    }
    checks = {
        item["name"]
        for item in inspector.get_check_constraints("ncc_weekly_report_runs")
    }
    assert "uq_ncc_weekly_report_runs_occurrence" in constraints
    assert "ck_ncc_weekly_runs_state_evidence" in checks
    marker_count = db_session.execute(
        text(
            "SELECT count(*) FROM domain_settings "
            "WHERE domain = 'notification' "
            "AND key = 'ncc_report_email_last_sent_local_date'"
        )
    ).scalar_one()
    assert marker_count == 0
