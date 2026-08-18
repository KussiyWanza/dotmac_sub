from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "deploy" / "observability" / "field_location_retention.rules.yml"
RUNBOOK = ROOT / "docs" / "runbooks" / "FIELD_LOCATION_RETENTION.md"


def test_location_retention_alerts_cover_liveness_failures_and_backlog() -> None:
    rules = RULES.read_text(encoding="utf-8")

    assert "FieldLocationRetentionRunnerMissing" in rules
    assert "FieldLocationRetentionRunnerStale" in rules
    assert "FieldLocationRetentionFailures" in rules
    assert "FieldLocationRetentionBacklog" in rules
    assert "observability_snapshot_age_seconds" in rules
    assert 'domain="field_location_retention"' in rules
    assert 'status="error"' in rules
    assert 'signal="batch_limit_reached"' in rules
    assert 'scope="global"' in rules
    assert "docs/runbooks/FIELD_LOCATION_RETENTION.md" in rules
    assert RUNBOOK.exists()
